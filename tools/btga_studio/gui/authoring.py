"""Scenario, item, bot and "Test in Game" windows (``AuthoringUI`` mixin for ``StudioApp``).

Everything here edits through the existing undo histories: scenarios and
item definitions through the project's ``DefinitionHistory`` (one
transaction per user action, typing and dragging coalesced), map records
(bots, pickups, targets, teleporter pairs) through the map document.  The
user picks records, items and models by name from lists filtered to what a
field accepts; internal IDs only appear in tooltips.

* Scenarios: an objective graph (columns follow ``requires``; drag nodes to
  arrange them), editors for triggers, timers, counters, waves, groups,
  start and end, rules and score, the map objects a scenario uses, and a
  Check tab with validation and the runtime features the scenario needs.
* Items: reusable definitions from presets, with model, icon, duration,
  stacking and a list of declarative effects.
* Inspector additions for bots (kind, model, role and the target each role
  needs), item pickups (which item) and destructible targets (models).
* Test in Game: available only when a compatible PC runtime is configured;
  checks everything (``core.runtime.preflight``) before it starts the
  runtime, and never falls back to Drive Preview.
"""

from __future__ import annotations

import time
from pathlib import Path

from imgui_bundle import imgui

from tools.btga_studio.core import actions as act
from tools.btga_studio.core import bots as bots_module
from tools.btga_studio.core import game_models
from tools.btga_studio.core import items as items_module
from tools.btga_studio.core import runtime as runtime_module
from tools.btga_studio.core import scenario as scn
from tools.btga_studio.core.document import EditError

RED = (1.0, 0.4, 0.35, 1.0)
AMBER = (1.0, 0.75, 0.3, 1.0)
GREEN = (0.45, 0.9, 0.45, 1.0)
GREY = (0.6, 0.6, 0.65, 1.0)
KIND_COLOURS = {"destroy": (0.75, 0.3, 0.25), "capture": (0.25, 0.45, 0.8), "defend": (0.2, 0.55, 0.65), "survive": (0.45, 0.45, 0.5),
                "escort": (0.55, 0.4, 0.75), "collect": (0.75, 0.6, 0.2), "race": (0.3, 0.6, 0.3), "kill_count": (0.65, 0.25, 0.4),
                "boss": (0.6, 0.15, 0.15), "reach": (0.3, 0.55, 0.45), "counter": (0.5, 0.5, 0.3), "trigger": (0.6, 0.5, 0.2),
                "marker_objective": (0.4, 0.4, 0.55)}
SCENARIO_TABS = ("Objectives graph", "Triggers", "Timers & counters", "Waves & groups", "Start & end", "Rules & score", "Map objects", "Check")
PLACEABLE = (("trigger_volume", "Trigger volume"), ("capture_zone", "Capture / defend zone"), ("destructible_target", "Destructible target"),
             ("bot", "Bot or Queenlord"), ("item_pickup", "Item pickup"), ("teleporter", "Teleporter entrance"), ("teleport_exit", "Teleport exit"))
NODE_W, NODE_H = 172.0, 58.0
COLUMN_GAP, ROW_GAP, FIRST_COLUMN = 205.0, 82.0, 128.0


def _vec(colour) -> imgui.ImVec4:
    return imgui.ImVec4(*colour)


def _u32(r, g, b, a=1.0) -> int:
    return imgui.get_color_u32(imgui.ImVec4(r, g, b, a))


class AuthoringUI:
    def init_authoring(self) -> None:
        self.show_scenarios = False
        self.show_items = False
        self.show_test_in_game = False
        self.scenario_key: str | None = None
        self.scenario_element: tuple[str, str] | None = None  # (family, element id)
        self.scenario_tab = 0
        self.item_key: str | None = None
        self.model_filter = ""
        self.launch = {"map": None, "scenario": None, "spawn": None, "bots": set(), "capture": "none"}
        self.launch_plan: runtime_module.LaunchPlan | None = None
        self.launch_checked = 0.0
        self.launch_status: str | None = None
        self._runtime_cache = (None, None, None)  # (path, mtime, (capabilities, problem))
        self._drag_node: tuple[str, float, float] | None = None

    # -- names and pickers ----------------------------------------------------------------
    def record_label(self, record: dict | None) -> str:
        if record is None:
            return "none"
        fields = record.get("fields", {})
        category = str(record.get("category", "object"))
        short = record["id"].rsplit(":", 1)[-1]
        if category == "bot_slot":
            return f"{bots_module.kind_name(record).capitalize()} bot slot {bots_module.value(record, 'ai_slot')} ({bots_module.role_name(record)}, team {bots_module.value(record, 'team')})"
        if category == "item_pickup":
            item = self.controller.project.items.get(bots_module.value(record, "item")) if self.controller.project else None
            return f"Pickup: {item.get('title') if item else 'no item'} #{short}"
        label = (fields.get("label") or {}).get("value")
        if label:
            return f"{label} ({category.replace('_', ' ')} #{short})"
        return f"{category.replace('_', ' ')} #{short}"

    def map_records(self, categories, map_key: str | None = None) -> list[dict]:
        c = self.controller
        if c.document is None or (map_key is not None and map_key != c.active_map):
            return []
        return [r for _, r in c.document.scene.records() if r.get("category") in categories and not r.get("disabled")]

    def record_combo(self, label: str, current, categories, apply, allow_none: bool = True, map_key: str | None = None) -> None:
        c = self.controller
        records = self.map_records(categories, map_key)
        found = c.document.scene.record(current) if (current and c.document is not None) else None
        preview = self.record_label(found[1]) if found else ("none" if not current else "missing object")
        if map_key is not None and map_key != c.active_map:
            imgui.text_disabled(f"{label.split('##')[0]}: {preview if current else 'none'} (open the scenario's map to change)")
            return
        if imgui.begin_combo(label, preview):
            if allow_none and imgui.selectable("none", current is None)[0]:
                apply(None)
            for record in records:
                if imgui.selectable(f"{self.record_label(record)}##{record['id']}", record["id"] == current)[0]:
                    apply(record["id"])
            if not records:
                imgui.text_disabled(f"No {', '.join(c_.replace('_', ' ') for c_ in categories[:3])} on this map yet")
            imgui.end_combo()
        if current and imgui.is_item_hovered():
            imgui.set_tooltip(str(current))

    def records_checklist(self, label: str, current: list, categories, apply, map_key: str | None = None, height: float = 110) -> None:
        records = self.map_records(categories, map_key)
        imgui.text(label.split("##")[0])
        if map_key is not None and map_key != self.controller.active_map:
            imgui.text_disabled(f"{len(current)} chosen (open the scenario's map to change)")
            return
        imgui.begin_child(f"##check{label}", imgui.ImVec2(0, height), imgui.ChildFlags_.borders)
        for record in records:
            on = record["id"] in current
            changed, on = imgui.checkbox(f"{self.record_label(record)}##{label}{record['id']}", on)
            if changed:
                apply([r for r in current if r != record["id"]] + ([record["id"]] if on else []))
        if not records:
            imgui.text_disabled("None on this map yet")
        imgui.end_child()

    def model_label(self, model_id: str | None) -> str:
        if not model_id:
            return "none"
        entry = game_models.BY_ID.get(model_id)
        if entry is not None:
            return entry["label"]
        return self.reference_name(model_id)

    def model_picker(self, label: str, current: str | None, roles, apply, allow_none: bool = True) -> None:
        c = self.controller
        library = c.library
        offered = game_models.offered(library, roles)
        missing = bool(current) and library is not None and current not in library._models
        preview = self.model_label(current) + ("  (not loaded!)" if missing else "")
        if imgui.begin_combo(label, preview):
            if allow_none and imgui.selectable("none", not current)[0]:
                apply("")
            if offered:
                imgui.separator_text("From your Global Assault cartridge")
            for entry in offered:
                if imgui.selectable(f"{entry['label']}  [{entry['evidence']}]##{entry['id']}", entry["id"] == current)[0]:
                    apply(entry["id"])
                if imgui.is_item_hovered():
                    imgui.set_tooltip(entry["note"])
            starter = (library.starter() or {}).get("models", {}) if library else {}
            if starter:
                imgui.separator_text("Studio starter")
                for name in ("tank_placeholder", "cube", "marker_destructible", "marker_item"):
                    if name in starter and imgui.selectable(f"{name.replace('_', ' ')}##{name}", starter[name] == current)[0]:
                        apply(starter[name])
            imgui.separator_text("Any model (search)")
            _, self.model_filter = imgui.input_text("##modelsearch", self.model_filter)
            needle = self.model_filter.lower().strip()
            if needle and c.index is not None:
                shown = 0
                for entry in c.index.catalog_entries:
                    if entry.kind != "model" or needle not in entry.label.lower():
                        continue
                    if imgui.selectable(f"{entry.label}##{entry.id}", entry.id == current)[0]:
                        apply(entry.id)
                    shown += 1
                    if shown >= 80:
                        imgui.text_disabled("... type more to narrow the list")
                        break
            imgui.end_combo()
        if current and imgui.is_item_hovered():
            note = game_models.BY_ID.get(current, {}).get("note", "")
            imgui.set_tooltip(f"{current}\n{note}".strip())

    # -- scenario edits through the undo history ------------------------------------------------
    def scenario_change(self, label: str, mutate, coalesce: str | None = None) -> bool:
        changed = self.change(label, mutate, coalesce, {"tab": "Scenarios", "window": "scenarios", "key": self.scenario_key})
        if changed:
            self.controller.request_diagnostics()
        return changed

    def s_text(self, label: str, data: dict, key: str, history: str, multiline: bool = False, limit: int = 120) -> None:
        current = str(data.get(key, "") or "")
        if multiline:
            changed, value = imgui.input_text_multiline(label, current, imgui.ImVec2(-1, 60))
        else:
            changed, value = imgui.input_text(label, current)
        if changed:
            self.scenario_change(history, lambda: data.__setitem__(key, value[:limit]), coalesce=f"text:{label}")
        self.seal_if_done()

    def s_int(self, label: str, data: dict, key: str, lo: int, hi: int, history: str, default: int = 0) -> None:
        changed, value = imgui.input_int(label, int(data.get(key, default) if isinstance(data.get(key, default), int) else default))
        if changed:
            self.scenario_change(history, lambda: data.__setitem__(key, max(lo, min(hi, int(value)))), coalesce=f"int:{label}")
        self.seal_if_done()

    def s_float(self, label: str, data: dict, key: str, lo: float, hi: float, history: str, default: float = 1.0) -> None:
        current = data.get(key, default)
        changed, value = imgui.slider_float(label, float(current if isinstance(current, (int, float)) else default), lo, hi, "%.2f")
        if changed:
            self.scenario_change(history, lambda: data.__setitem__(key, round(float(value), 2)), coalesce=f"float:{label}")
        self.seal_if_done()

    def s_bool(self, label: str, data: dict, key: str, history: str, default: bool = False) -> None:
        changed, value = imgui.checkbox(label, bool(data.get(key, default)))
        if changed:
            self.scenario_change(history, lambda: data.__setitem__(key, bool(value)))

    def s_combo(self, label: str, data: dict, key: str, options, history: str, names=None) -> None:
        options = list(options)
        current = data.get(key)
        shown = (names or {}).get(current, str(current).replace("_", " ") if current is not None else "none")
        if imgui.begin_combo(label, shown):
            for option in options:
                if imgui.selectable(f"{(names or {}).get(option, str(option).replace('_', ' '))}##{option}", option == current)[0]:
                    self.scenario_change(history, lambda o=option: data.__setitem__(key, o))
            imgui.end_combo()

    def element_combo(self, label: str, data: dict, key: str, scenario: dict, family: str, history: str) -> None:
        table = scenario.get(family) or {}
        names = {k: v.get("title", k) for k, v in table.items() if isinstance(v, dict)}
        self.s_combo(label, data, key, [None] + sorted(table), history, {None: "none", **names})

    # -- action lists -----------------------------------------------------------------------
    def action_list(self, label: str, owner: dict, key: str, context: str, scenario: dict | None, change) -> None:
        actions = owner.get(key)
        if not isinstance(actions, list):
            actions = []
        imgui.text(label.split("##")[0])
        names = self._element_names(scenario)
        for number, action in enumerate(list(actions)):
            imgui.push_id(f"{label}{number}")
            imgui.bullet()
            imgui.text_wrapped(act.describe(action, names) if isinstance(action, dict) else "malformed action")
            spec = act.ACTIONS.get(action.get("do")) if isinstance(action, dict) else None
            if spec is not None and imgui.tree_node("edit"):
                for name, kind in spec["params"].items():
                    self._action_param(name, kind, action, scenario, change)
                imgui.tree_pop()
            imgui.same_line()
            if imgui.small_button("Up") and number > 0:
                change("Reorder actions", lambda n=number: actions.insert(n - 1, actions.pop(n)))
            imgui.same_line()
            if imgui.small_button("Remove"):
                change("Remove action", lambda n=number: actions.pop(n))
            imgui.pop_id()
        allowed = [k for k, v in act.ACTIONS.items() if context in v["contexts"]]
        if imgui.begin_combo(f"Add action##{label}", "add an action..."):
            for kind in allowed:
                if imgui.selectable(act.ACTIONS[kind]["label"], False)[0]:
                    change("Add action", lambda k=kind: owner.setdefault(key, []).append(act.new_action(k)))
            imgui.end_combo()

    def _element_names(self, scenario: dict | None) -> dict:
        names = {}
        if scenario:
            for family in scn.FAMILIES:
                for element_id, element in (scenario.get(family) or {}).items():
                    if isinstance(element, dict):
                        names[element_id] = element.get("title", element_id)
        if self.controller.project:
            names.update({k: v.get("title", k) for k, v in self.controller.project.items.items() if isinstance(v, dict)})
        doc = self.controller.document
        if doc is not None:
            for _, record in doc.scene.records("markers"):
                if record.get("category") in ("trigger_volume", "capture_zone", "destructible_target", "bot_slot", "item_pickup"):
                    names[record["id"]] = self.record_label(record)
        return names

    def _action_param(self, name: str, kind: tuple, action: dict, scenario: dict | None, change) -> None:
        label = f"{name.replace('_', ' ')}##{id(action)}{name}"
        if kind[0] == "enum":
            current = action.get(name)
            if imgui.begin_combo(label, str(current).replace("_", " ")):
                for option in kind[1]:
                    if imgui.selectable(option.replace("_", " "), option == current)[0]:
                        change("Edit action", lambda o=option: action.__setitem__(name, o))
                imgui.end_combo()
        elif kind[0] == "int":
            changed, value = imgui.input_int(label, int(action.get(name) or 0))
            if changed:
                change("Edit action", lambda: action.__setitem__(name, max(kind[1], min(kind[2], int(value)))), f"act:{id(action)}{name}")
            self.seal_if_done()
        elif kind[0] == "float":
            changed, value = imgui.slider_float(label, float(action.get(name) or kind[1]), kind[1], kind[2], "%.2f")
            if changed:
                change("Edit action", lambda: action.__setitem__(name, round(float(value), 2)), f"act:{id(action)}{name}")
            self.seal_if_done()
        elif kind[0] == "text":
            changed, value = imgui.input_text(label, str(action.get(name) or ""))
            if changed:
                change("Edit action", lambda: action.__setitem__(name, value[:kind[1]]), f"act:{id(action)}{name}")
            self.seal_if_done()
        elif kind[0] == "sound":
            current = action.get(name)
            if imgui.begin_combo(label, str(current).rsplit(":", 1)[-1]):
                for sound in act.STUDIO_SOUNDS:
                    if imgui.selectable(sound.replace("_", " "), current == f"studio:sound:{sound}")[0]:
                        change("Edit action", lambda s=sound: action.__setitem__(name, f"studio:sound:{s}"))
                imgui.end_combo()
        elif kind[0] == "ref":
            family = kind[1]
            if family == "item":
                items = self.controller.project.items if self.controller.project else {}
                options = {k: v.get("title", k) for k, v in items.items()}
            elif family == "record_or_group":
                options = {k: v.get("title", k) for k, v in ((scenario or {}).get("groups") or {}).items()}
                for record in self.map_records(("bot_slot", "destructible_target", "trigger_volume", "capture_zone", "item_pickup", "teleporter",
                                                "unit_spawner", "hazard")):
                    options[record["id"]] = self.record_label(record)
            else:
                table = (scenario or {}).get({"objective": "objectives", "trigger": "triggers", "timer": "timers", "counter": "counters",
                                              "wave": "waves", "group": "groups"}.get(family, ""), {}) or {}
                options = {k: v.get("title", k) for k, v in table.items() if isinstance(v, dict)}
            current = action.get(name)
            if imgui.begin_combo(label, options.get(current, "choose..." if not current else f"missing {current}")):
                for option, title in options.items():
                    if imgui.selectable(f"{title}##{option}", option == current)[0]:
                        change("Edit action", lambda o=option: action.__setitem__(name, o))
                if not options:
                    imgui.text_disabled(f"Add a {family.replace('_or_', ' or ')} first")
                imgui.end_combo()

    # -- the scenario window ---------------------------------------------------------------------
    def open_scenarios(self, key: str | None = None) -> None:
        self.show_scenarios = True
        if key:
            self.scenario_key = key

    def window_scenarios(self) -> None:
        self.bring_to_front("scenarios", self.show_scenarios)
        if not self.show_scenarios:
            return
        c = self.controller
        imgui.set_next_window_size(imgui.ImVec2(1480, 760), imgui.Cond_.appearing)
        imgui.set_next_window_pos(imgui.ImVec2(40, 60), imgui.Cond_.appearing)
        imgui.set_next_window_bg_alpha(1.0)
        opened, self.show_scenarios = imgui.begin("Scenario editor###scenarios", self.show_scenarios, imgui.WindowFlags_.no_collapse)
        if opened:
            if c.project is None:
                imgui.text_disabled("Open or create a project to write scenarios.")
            else:
                self.scenario_list()
                imgui.same_line()
                imgui.begin_child("scenario-main", imgui.ImVec2(0, 0))
                scenario = c.project.scenarios.get(self.scenario_key) if self.scenario_key else None
                if scenario is None:
                    imgui.text_disabled("Choose a scenario on the left, or make a new one for the open map.")
                else:
                    self.scenario_header(scenario)
                imgui.end_child()
        imgui.end()

    def scenario_list(self) -> None:
        c = self.controller
        project = c.project
        imgui.begin_child("scenario-list", imgui.ImVec2(230, 0), imgui.ChildFlags_.borders)
        self.history_buttons()
        imgui.separator()
        for key, scenario in sorted(project.scenarios.items()):
            if not isinstance(scenario, dict):
                continue
            map_title = project.maps[scenario["map"]].title if scenario.get("map") in project.maps else "missing map"
            if imgui.selectable(f"{scenario.get('title', key)}\n  {map_title}##sc{key}", key == self.scenario_key)[0]:
                self.scenario_key, self.scenario_element = key, None
                if scenario.get("map") in project.maps and scenario["map"] != c.active_map:
                    self.switch_map(scenario["map"])
        imgui.separator()
        imgui.begin_disabled(c.active_map is None)
        if imgui.begin_combo("##newscenario", "New scenario..."):
            for mode in scn.MODES:
                if imgui.selectable(mode.replace("_", " ").capitalize(), False)[0]:
                    key = scn.next_id("scenario_", project.scenarios)
                    title = f"{mode.replace('_', ' ').capitalize()} {len(project.scenarios) + 1}"
                    if self.scenario_change("New scenario", lambda k=key, m=mode, t=title: project.define(
                            "scenarios", k, scn.new_scenario(t, c.active_map, m))):
                        self.scenario_key = key
            imgui.end_combo()
        imgui.end_disabled()
        if self.scenario_key in project.scenarios and imgui.small_button("Delete scenario"):
            key = self.scenario_key
            self.confirm("Delete scenario", f"Delete the scenario \"{project.scenarios[key].get('title', key)}\"? You can undo this.", "Delete",
                         lambda k=key: self.scenario_change("Delete scenario", lambda: project.scenarios.pop(k)))
        imgui.end_child()

    def scenario_header(self, scenario: dict) -> None:
        c = self.controller
        project = c.project
        problems = self.scenario_problems()
        imgui.set_next_item_width(320)
        self.s_text("Name##scenario", scenario, "title", "Rename scenario", limit=80)
        imgui.same_line()
        maps = sorted(project.maps)
        imgui.set_next_item_width(220)
        self.s_combo("Map##scenario", scenario, "map", maps, "Change scenario map", {k: project.maps[k].title for k in maps})
        imgui.same_line()
        rules = scenario.setdefault("rules", {})
        imgui.set_next_item_width(200)
        self.s_combo("Mode##scenario", rules, "mode", scn.MODES, "Change match mode")
        errors = [p for p in problems if p.severity == "error"]
        imgui.text_colored(_vec(RED if errors else GREEN), f"{len(errors)} problem(s)" if errors else "No problems found")
        imgui.same_line()
        status = runtime_module.playability({f: [] for f in scn.required_features(scenario)}, self.controller.runtime_capabilities())
        if status["status"] == "unverified":
            text = "Runtime support: not checked (no PC runtime configured)"
        elif status["status"] == "supported":
            text = f"Runtime support: {status['runtime']['name']} declares every feature"
        else:
            text = f"Runtime support: {len(status['missing'])} feature(s) missing in {status['runtime']['name']}"
        imgui.text_colored(_vec(AMBER if status["status"] != "supported" else GREEN), text)
        if scenario.get("map") != c.active_map:
            imgui.text_colored(_vec(AMBER), "This scenario's map is not open: object pickers are read-only.")
            imgui.same_line()
            if scenario.get("map") in project.maps and imgui.small_button("Open its map"):
                self.switch_map(scenario["map"])
        if imgui.begin_tab_bar("scenario-tabs"):
            for number, name in enumerate(SCENARIO_TABS):
                flags = imgui.TabItemFlags_.set_selected if getattr(self, "_force_tab", None) == name else 0
                if imgui.begin_tab_item(name, None, flags)[0]:
                    self.scenario_tab = number
                    getattr(self, "scenario_tab_" + name.split()[0].lower())(scenario, problems)
                    imgui.end_tab_item()
            imgui.end_tab_bar()

    def scenario_problems(self) -> list:
        c = self.controller
        scenario = c.project.scenarios.get(self.scenario_key)
        if scenario is None:
            return []
        document = c.document if scenario.get("map") == c.active_map else None
        ctx = scn.context_for(c.project, self.scenario_key, document, c.library, c.runtime_capabilities())
        return scn.check(self.scenario_key, scenario, ctx)

    # objectives graph ------------------------------------------------------------------------
    def graph_layout(self, scenario: dict) -> dict[str, tuple[float, float]]:
        objectives = scenario.get("objectives") or {}
        depth = scn.depth(objectives)
        rows: dict[int, int] = {}
        out = {}
        for element_id in sorted(objectives, key=lambda k: (depth.get(k, 0), k)):
            column = depth.get(element_id, 0)
            row = rows.get(column, 0)
            rows[column] = row + 1
            saved = (scenario.get("layout") or {}).get(element_id)
            out[element_id] = (float(saved[0]), float(saved[1])) if isinstance(saved, list) and len(saved) == 2 else \
                (FIRST_COLUMN + column * COLUMN_GAP, 30.0 + row * ROW_GAP)
        return out

    def scenario_tab_objectives(self, scenario: dict, problems) -> None:
        objectives = scenario.setdefault("objectives", {})
        if imgui.begin_combo("##addobjective", "Add objective..."):
            for kind, spec in scn.OBJECTIVE_KINDS.items():
                if imgui.selectable(spec["label"], False)[0]:
                    def add(k=kind):
                        element = scn.add_element(scenario, "objectives", k)
                        self.scenario_element = ("objectives", element)
                    self.scenario_change("Add objective", add)
            imgui.end_combo()
        imgui.same_line()
        imgui.text_disabled("Arrows show 'requires'. Dashed: optional. Drag a box to move it; click to edit it on the right.")
        avail = imgui.get_content_region_avail()
        imgui.begin_child("graph", imgui.ImVec2(max(300.0, avail.x - 350), 0), imgui.ChildFlags_.borders,
                          imgui.WindowFlags_.horizontal_scrollbar)
        self.draw_graph(scenario, problems)
        imgui.end_child()
        imgui.same_line()
        imgui.begin_child("objective-editor", imgui.ImVec2(0, 0), imgui.ChildFlags_.borders)
        selected = self.scenario_element[1] if self.scenario_element and self.scenario_element[0] == "objectives" else None
        if selected in objectives:
            self.objective_editor(scenario, selected, objectives[selected], problems)
        else:
            imgui.text_wrapped("Select an objective in the graph. Objectives with no requirement start at once and run in parallel; "
                               "chain them with 'requires' for an ordered sequence.")
        imgui.end_child()

    def draw_graph(self, scenario: dict, problems) -> None:
        objectives = scenario.get("objectives") or {}
        draw = imgui.get_window_draw_list()
        origin = imgui.get_cursor_screen_pos()
        scroll = (imgui.get_scroll_x(), imgui.get_scroll_y())
        positions = self.graph_layout(scenario)
        if self._drag_node and self._drag_node[0] in positions:
            positions[self._drag_node[0]] = (self._drag_node[1], self._drag_node[2])
        width = max([x for x, _ in positions.values()] + [0.0]) + NODE_W + 160
        height = max([y for _, y in positions.values()] + [0.0]) + NODE_H + 40
        bad = {}
        for problem in problems:
            parts = (problem.field or "").split(".")
            if len(parts) >= 2 and parts[0] == "objectives":
                bad.setdefault(parts[1], []).append(problem)
        loop = scn.requirement_cycle({k: v for k, v in objectives.items() if isinstance(v, dict)}) or []
        loop_edges = set(zip(loop[:-1], loop[1:]))  # (needs, needed) pairs that close the loop
        for element_id in loop[:-1]:
            bad.setdefault(element_id, [])
            if not any(p.code == "OBJECTIVE_CYCLE" for p in bad[element_id]):
                bad[element_id].append(scn.Problem("error", "OBJECTIVE_CYCLE", "", f"objectives.{element_id}.requires",
                                                   "part of a requirement loop: " + " -> ".join(loop)))

        def screen(x, y):
            return imgui.ImVec2(origin.x + x, origin.y + y)

        start = (20.0, 30.0)
        victory_x = width - 125
        draw.add_rect_filled(screen(*start), screen(start[0] + 80, start[1] + 40), _u32(0.2, 0.5, 0.25), 6)
        draw.add_text(screen(start[0] + 18, start[1] + 12), _u32(1, 1, 1), "START")
        dependents = {r for o in objectives.values() if isinstance(o, dict) for r in o.get("requires") or []}
        draw.add_rect_filled(screen(victory_x, 30), screen(victory_x + 110, 70), _u32(0.55, 0.45, 0.1), 6)
        draw.add_text(screen(victory_x + 22, 42), _u32(1, 1, 1), "VICTORY")

        def arrow(a, b, colour, thickness=2.0):
            if b.x > a.x:
                draw.add_bezier_cubic(a, imgui.ImVec2(a.x + 60, a.y), imgui.ImVec2(b.x - 60, b.y), b, colour, thickness)
            else:  # a backward link (only possible in a loop): swing below the boxes so it stays visible
                low = max(a.y, b.y) + NODE_H + 30
                draw.add_bezier_cubic(a, imgui.ImVec2(a.x + 90, low), imgui.ImVec2(b.x - 90, low), b, colour, thickness)
            draw.add_triangle_filled(b, imgui.ImVec2(b.x - 9, b.y - 5), imgui.ImVec2(b.x - 9, b.y + 5), colour)

        for element_id, objective in objectives.items():
            if not isinstance(objective, dict):
                continue
            x, y = positions[element_id]
            target = screen(x, y + NODE_H / 2)
            requires = [r for r in objective.get("requires") or [] if r in positions]
            if not requires:
                arrow(screen(start[0] + 80, start[1] + 20), target, _u32(0.5, 0.8, 0.5, 0.8))
            for required in requires:
                rx, ry = positions[required]
                looped = (element_id, required) in loop_edges
                arrow(screen(rx + NODE_W, ry + NODE_H / 2), target, _u32(1, 0.35, 0.3) if looped else _u32(0.85, 0.85, 0.9, 0.9),
                      3.0 if looped else 2.0)
            if element_id not in dependents and not objective.get("optional"):
                arrow(screen(x + NODE_W, y + NODE_H / 2), screen(victory_x, 50), _u32(0.9, 0.75, 0.3, 0.7))
        selected = self.scenario_element[1] if self.scenario_element and self.scenario_element[0] == "objectives" else None
        for element_id, objective in sorted(objectives.items()):
            if not isinstance(objective, dict):
                continue
            x, y = positions[element_id]
            colour = KIND_COLOURS.get(objective.get("kind"), (0.4, 0.4, 0.4))
            alpha = 0.55 if objective.get("optional") else 0.95
            a, b = screen(x, y), screen(x + NODE_W, y + NODE_H)
            draw.add_rect_filled(a, b, _u32(*colour, alpha), 6)
            border = _u32(1, 0.35, 0.3) if element_id in bad else (_u32(1, 0.95, 0.3) if element_id == selected else _u32(0.9, 0.9, 0.95, 0.6))
            thickness = 3.0 if element_id in bad or element_id == selected else 1.0
            if objective.get("optional"):
                for k in range(0, int(NODE_W), 12):  # dashed outline for optional objectives
                    draw.add_line(screen(x + k, y), screen(x + min(k + 6, NODE_W), y), border, thickness)
                    draw.add_line(screen(x + k, y + NODE_H), screen(x + min(k + 6, NODE_W), y + NODE_H), border, thickness)
            else:
                draw.add_rect(a, b, border, 6.0, thickness)
            kind = scn.OBJECTIVE_KINDS.get(objective.get("kind"), {}).get("label", objective.get("kind"))
            repeat = objective.get("repeat", 1)
            badge = "" if repeat == 1 else ("  x whole match" if repeat == 0 else f"  x{repeat}")
            draw.add_text(screen(x + 8, y + 6), _u32(1, 1, 1), str(objective.get("title", element_id))[:26])
            draw.add_text(screen(x + 8, y + 24), _u32(0.92, 0.92, 0.95, 0.9), f"{kind}{badge}"[:30])
            if element_id in bad:
                draw.add_text(screen(x + 8, y + 40), _u32(1, 0.8, 0.75), f"{len(bad[element_id])} problem(s)")
            elif objective.get("optional"):
                draw.add_text(screen(x + 8, y + 40), _u32(0.9, 0.9, 0.9, 0.8), "optional")
            imgui.set_cursor_screen_pos(a)
            imgui.invisible_button(f"node{element_id}", imgui.ImVec2(NODE_W, NODE_H))
            if imgui.is_item_activated():
                self.scenario_element = ("objectives", element_id)
            if imgui.is_item_active() and imgui.is_mouse_dragging(imgui.MouseButton_.left, 4.0):
                delta = imgui.get_io().mouse_delta
                current = self._drag_node[1:] if self._drag_node and self._drag_node[0] == element_id else (x, y)
                self._drag_node = (element_id, max(0.0, current[0] + delta.x), max(0.0, current[1] + delta.y))
            if imgui.is_item_deactivated() and self._drag_node and self._drag_node[0] == element_id:
                _, nx, ny = self._drag_node
                self._drag_node = None
                self.scenario_change("Move objective box", lambda e=element_id, p=[round(nx), round(ny)]: scenario.setdefault("layout", {}).__setitem__(e, p))
            if imgui.is_item_hovered() and not imgui.is_item_active():
                tips = [f"{p.message}" for p in bad.get(element_id, [])]
                imgui.set_tooltip("\n".join([str(objective.get("title", element_id)), *tips]))
        imgui.set_cursor_screen_pos(screen(0, 0))
        imgui.dummy(imgui.ImVec2(width + scroll[0] * 0, height))

    def objective_editor(self, scenario: dict, element_id: str, objective: dict, problems) -> None:
        objectives = scenario["objectives"]
        imgui.text(f"Objective {element_id}")
        self.s_text("Name##obj", objective, "title", "Rename objective", limit=80)
        kind = objective.get("kind")
        if imgui.begin_combo("Kind##obj", scn.OBJECTIVE_KINDS.get(kind, {}).get("label", str(kind))):
            for option, spec in scn.OBJECTIVE_KINDS.items():
                if imgui.selectable(spec["label"], option == kind)[0] and option != kind:
                    self.scenario_change("Change objective kind", lambda o=option: objective.update(
                        {"kind": o, "params": scn.new_objective(o)["params"]}))
            imgui.end_combo()
        spec = scn.OBJECTIVE_KINDS.get(kind)
        params = objective.setdefault("params", {})
        map_key = scenario.get("map")
        if spec:
            if spec.get("help"):
                imgui.text_disabled(spec["help"])
            for name, kind_spec in spec["params"].items():
                label = f"{name.replace('_', ' ').capitalize()}##p{element_id}{name}"
                if kind_spec[0] == "int":
                    self.s_int(label, params, name, kind_spec[1], kind_spec[2], "Edit objective")
                elif kind_spec[0] == "record":
                    self.record_combo(label, params.get(name), kind_spec[1], lambda v, n=name: self.scenario_change(
                        "Edit objective", lambda: params.__setitem__(n, v)), map_key=map_key)
                elif kind_spec[0] == "records":
                    self.records_checklist(label, list(params.get(name) or []), kind_spec[1], lambda v, n=name: self.scenario_change(
                        "Edit objective", lambda: params.__setitem__(n, v)), map_key=map_key)
                elif kind_spec[0] == "item":
                    items = self.controller.project.items
                    self.s_combo(label, params, name, [None] + sorted(items), "Edit objective",
                                 {None: "choose an item", **{k: v.get("title", k) for k, v in items.items()}})
                elif kind_spec[0] in ("counter", "trigger"):
                    self.element_combo(label, params, name, scenario, kind_spec[0] + "s", "Edit objective")
                elif kind_spec[0] in ("team", "team_or_any"):
                    teams = int((scenario.get("rules") or {}).get("teams", 2) or 1)
                    options = ([-1] if kind_spec[0] == "team_or_any" else []) + list(range(teams))
                    self.s_combo(label, params, name, options, "Edit objective", {-1: "any team", **{t: f"team {t}" for t in range(teams)}})
        imgui.separator_text("Flow")
        self.s_bool("Optional (not needed to win)##obj", objective, "optional", "Change optional")
        self.s_int("Repeat (1 = once, 0 = whole match)##obj", objective, "repeat", 0, 99, "Change repeat", 1)
        self.s_bool("Hidden until it starts##obj", objective, "hidden", "Change hidden")
        imgui.text("Requires (all of these first):")
        requires = list(objective.get("requires") or [])
        for other_id, other in sorted(objectives.items()):
            if other_id == element_id or not isinstance(other, dict):
                continue
            trial = {k: dict(v, requires=list(v.get("requires") or []) + ([other_id] if k == element_id else [])) for k, v in objectives.items()
                     if isinstance(v, dict)}
            loops = other_id not in requires and scn.requirement_cycle(trial) is not None
            imgui.begin_disabled(loops)
            changed, on = imgui.checkbox(f"{other.get('title', other_id)}##req{element_id}{other_id}", other_id in requires)
            imgui.end_disabled()
            if loops and imgui.is_item_hovered(imgui.HoveredFlags_.allow_when_disabled):
                imgui.set_tooltip("It already needs this objective first: requiring it here would make a loop")
            if changed:
                self.scenario_change("Change requirements", lambda o=other_id, v=on: objective.__setitem__(
                    "requires", [r for r in objective.get("requires") or [] if r != o] + ([o] if v else [])))
        imgui.separator_text("When it completes")
        self.action_list(f"On complete##{element_id}", objective, "on_complete", "scenario", scenario, self._scenario_action_change)
        imgui.separator_text("When it fails")
        self.action_list(f"On fail##{element_id}", objective, "on_fail", "scenario", scenario, self._scenario_action_change)
        mine = [p for p in problems if (p.field or "").startswith(f"objectives.{element_id}")]
        for problem in mine:
            imgui.text_colored(_vec(RED if problem.severity == "error" else AMBER), f"{problem.code}: {problem.message}")
        if imgui.button("Delete objective"):
            self.scenario_change("Delete objective", lambda: scn.remove_element(scenario, "objectives", element_id))
            self.scenario_element = None

    def _scenario_action_change(self, label, mutate, coalesce=None):
        return self.scenario_change(label, mutate, coalesce)

    # triggers, timers, counters, waves, groups ------------------------------------------------
    def element_list(self, scenario: dict, family: str, noun: str) -> str | None:
        table = scenario.setdefault(family, {})
        selected = self.scenario_element[1] if self.scenario_element and self.scenario_element[0] == family else None
        imgui.begin_child(f"list-{family}", imgui.ImVec2(240, 0), imgui.ChildFlags_.borders)
        for element_id, element in sorted(table.items()):
            if imgui.selectable(f"{(element or {}).get('title', element_id)}##{family}{element_id}", element_id == selected)[0]:
                self.scenario_element = (family, element_id)
                selected = element_id
        if imgui.button(f"New {noun}"):
            def add():
                element = scn.add_element(scenario, family)
                self.scenario_element = (family, element)
            self.scenario_change(f"Add {noun}", add)
        if selected in table and imgui.small_button(f"Delete {noun}"):
            self.scenario_change(f"Delete {noun}", lambda: scn.remove_element(scenario, family, selected))
            self.scenario_element = None
            selected = None
        imgui.end_child()
        imgui.same_line()
        return selected

    def problems_for(self, problems, prefix: str) -> None:
        for problem in problems:
            if (problem.field or "").startswith(prefix):
                imgui.text_colored(_vec(RED if problem.severity == "error" else AMBER), f"{problem.code}: {problem.message}")

    def scenario_tab_triggers(self, scenario: dict, problems) -> None:
        selected = self.element_list(scenario, "triggers", "trigger")
        imgui.begin_child("trigger-editor", imgui.ImVec2(0, 0), imgui.ChildFlags_.borders)
        trigger = (scenario.get("triggers") or {}).get(selected)
        if trigger is None:
            imgui.text_wrapped("A trigger fires its actions when something happens: a tank enters, leaves or stays in a trigger volume or "
                               "zone, the scenario starts, an objective completes or fails, a timer runs out, a counter reaches a value "
                               "or an object is destroyed.")
        else:
            self.s_text("Name##trg", trigger, "title", "Rename trigger", limit=80)
            self.s_combo("When##trg", trigger, "on", scn.TRIGGER_EVENTS, "Change trigger event")
            event = trigger.get("on")
            if event in scn.VOLUME_EVENTS:
                self.record_combo("Area##trg", trigger.get("volume"), scn.VOLUME_CATEGORIES, lambda v: self.scenario_change(
                    "Change trigger area", lambda: trigger.__setitem__("volume", v)), map_key=scenario.get("map"))
                self.s_combo("Who##trg", trigger, "subject", scn.SUBJECTS, "Change trigger subject")
                if event == "inside":
                    self.s_int("Seconds inside##trg", trigger, "seconds", 1, 3600, "Change trigger time", 5)
            elif event in ("objective_completed", "objective_failed"):
                self.element_combo("Objective##trg", trigger, "objective", scenario, "objectives", "Change trigger objective")
            elif event == "timer_expired":
                self.element_combo("Timer##trg", trigger, "timer", scenario, "timers", "Change trigger timer")
            elif event == "counter_reached":
                self.element_combo("Counter##trg", trigger, "counter", scenario, "counters", "Change trigger counter")
                self.s_int("Value##trg", trigger, "value", -100000, 100000, "Change trigger value")
            elif event == "destroyed":
                self.record_combo("Object##trg", trigger.get("record"), scn.DESTRUCTIBLE_CATEGORIES, lambda v: self.scenario_change(
                    "Change trigger object", lambda: trigger.__setitem__("record", v)), map_key=scenario.get("map"))
            self.s_bool("Fires only once##trg", trigger, "once", "Change trigger once", True)
            self.s_bool("Switched on at the start##trg", trigger, "enabled", "Change trigger enabled", True)
            imgui.separator_text("Actions")
            self.action_list(f"Actions##{selected}", trigger, "actions", "scenario", scenario, self._scenario_action_change)
            self.problems_for(problems, f"triggers.{selected}")
        imgui.end_child()

    def scenario_tab_timers(self, scenario: dict, problems) -> None:
        imgui.begin_child("timers", imgui.ImVec2(imgui.get_content_region_avail().x / 2, 0), imgui.ChildFlags_.borders)
        imgui.text("Timers")
        for element_id, timer in sorted((scenario.setdefault("timers", {})).items()):
            if imgui.tree_node(f"{timer.get('title', element_id)}##tm{element_id}"):
                self.s_text(f"Name##tm{element_id}", timer, "title", "Rename timer", limit=80)
                self.s_int(f"Seconds##tm{element_id}", timer, "seconds", 1, 7200, "Change timer length", 60)
                self.s_combo(f"Starts##tm{element_id}", timer, "starts", ("at_start", "by_action"), "Change timer start",
                             {"at_start": "when the scenario starts", "by_action": "when an action starts it"})
                self.s_bool(f"Repeats##tm{element_id}", timer, "repeat", "Change timer repeat")
                self.s_bool(f"Shown to the player##tm{element_id}", timer, "visible", "Change timer visibility", True)
                self.action_list(f"When it runs out##{element_id}", timer, "on_expire", "scenario", scenario, self._scenario_action_change)
                self.problems_for(problems, f"timers.{element_id}")
                if imgui.small_button(f"Delete timer##{element_id}"):
                    self.scenario_change("Delete timer", lambda e=element_id: scn.remove_element(scenario, "timers", e))
                imgui.tree_pop()
        if imgui.button("New timer"):
            self.scenario_change("Add timer", lambda: scn.add_element(scenario, "timers"))
        imgui.end_child()
        imgui.same_line()
        imgui.begin_child("counters", imgui.ImVec2(0, 0), imgui.ChildFlags_.borders)
        imgui.text("Counters")
        for element_id, counter in sorted((scenario.setdefault("counters", {})).items()):
            if imgui.tree_node(f"{counter.get('title', element_id)}##ct{element_id}"):
                self.s_text(f"Name##ct{element_id}", counter, "title", "Rename counter", limit=80)
                for name in ("initial", "min", "max"):
                    self.s_int(f"{name.capitalize()}##ct{element_id}", counter, name, -100000, 100000, "Change counter")
                self.s_bool(f"Shown to the player##ct{element_id}", counter, "visible", "Change counter visibility", True)
                self.problems_for(problems, f"counters.{element_id}")
                if imgui.small_button(f"Delete counter##{element_id}"):
                    self.scenario_change("Delete counter", lambda e=element_id: scn.remove_element(scenario, "counters", e))
                imgui.tree_pop()
        if imgui.button("New counter"):
            self.scenario_change("Add counter", lambda: scn.add_element(scenario, "counters"))
        imgui.end_child()

    def scenario_tab_waves(self, scenario: dict, problems) -> None:
        map_key = scenario.get("map")
        imgui.begin_child("waves", imgui.ImVec2(imgui.get_content_region_avail().x / 2, 0), imgui.ChildFlags_.borders)
        imgui.text("Waves (bots or a unit generator sent by an action)")
        for element_id, wave in sorted((scenario.setdefault("waves", {})).items()):
            if imgui.tree_node(f"{wave.get('title', element_id)}##wv{element_id}"):
                self.s_text(f"Name##wv{element_id}", wave, "title", "Rename wave", limit=80)
                self.records_checklist(f"Bots##wv{element_id}", list(wave.get("bots") or []), ("bot_slot",), lambda v, w=wave: self.scenario_change(
                    "Change wave bots", lambda: w.__setitem__("bots", v)), map_key=map_key, height=80)
                self.record_combo(f"Unit generator##wv{element_id}", wave.get("spawner"), ("unit_spawner",), lambda v, w=wave: self.scenario_change(
                    "Change wave generator", lambda: w.__setitem__("spawner", v)), map_key=map_key)
                for name, lo, hi in (("count", 1, 64), ("interval_seconds", 0, 600), ("delay_seconds", 0, 3600), ("repeat", 0, 99)):
                    self.s_int(f"{name.replace('_', ' ').capitalize()}##wv{element_id}", wave, name, lo, hi, "Change wave")
                self.problems_for(problems, f"waves.{element_id}")
                if imgui.small_button(f"Delete wave##{element_id}"):
                    self.scenario_change("Delete wave", lambda e=element_id: scn.remove_element(scenario, "waves", e))
                imgui.tree_pop()
        if imgui.button("New wave"):
            self.scenario_change("Add wave", lambda: scn.add_element(scenario, "waves"))
        imgui.end_child()
        imgui.same_line()
        imgui.begin_child("groups", imgui.ImVec2(0, 0), imgui.ChildFlags_.borders)
        imgui.text("Groups (switched on and off together)")
        everything = ("bot_slot", "destructible_target", "trigger_volume", "capture_zone", "item_pickup", "teleporter", "teleport_exit", "hazard",
                      "unit_spawner", "pickup")
        for element_id, group in sorted((scenario.setdefault("groups", {})).items()):
            if imgui.tree_node(f"{group.get('title', element_id)}##gp{element_id}"):
                self.s_text(f"Name##gp{element_id}", group, "title", "Rename group", limit=80)
                self.records_checklist(f"Members##gp{element_id}", list(group.get("members") or []), everything,
                                       lambda v, gr=group: self.scenario_change("Change group members", lambda: gr.__setitem__("members", v)),
                                       map_key=map_key)
                self.s_bool(f"Active at the start##gp{element_id}", group, "active_at_start", "Change group start", True)
                self.problems_for(problems, f"groups.{element_id}")
                if imgui.small_button(f"Delete group##{element_id}"):
                    self.scenario_change("Delete group", lambda e=element_id: scn.remove_element(scenario, "groups", e))
                imgui.tree_pop()
        if imgui.button("New group"):
            self.scenario_change("Add group", lambda: scn.add_element(scenario, "groups"))
        imgui.end_child()

    def scenario_tab_start(self, scenario: dict, problems) -> None:
        project = self.controller.project
        start = scenario.setdefault("start", {})
        end = scenario.setdefault("end", {})
        imgui.begin_child("start", imgui.ImVec2(imgui.get_content_region_avail().x / 2, 0), imgui.ChildFlags_.borders)
        imgui.separator_text("Start")
        self.record_combo("Player starts at##start", start.get("player_spawn"), scn.SPAWN_CATEGORIES, lambda v: self.scenario_change(
            "Change player spawn", lambda: start.__setitem__("player_spawn", v)), map_key=scenario.get("map"))
        self.s_combo("Opening##start", start, "transition", scn.TRANSITIONS_START, "Change opening")
        self.s_text("Briefing##start", start, "briefing", "Edit briefing", multiline=True, limit=600)
        self.action_list("At the start##startactions", start, "actions", "scenario", scenario, self._scenario_action_change)
        imgui.separator_text("Victory when any of these happens")
        self.condition_list(scenario, "victory", problems)
        imgui.end_child()
        imgui.same_line()
        imgui.begin_child("end", imgui.ImVec2(0, 0), imgui.ChildFlags_.borders)
        imgui.separator_text("Failure when any of these happens")
        self.condition_list(scenario, "failure", problems)
        imgui.separator_text("Ending")
        victory = end.setdefault("victory", {})
        failure = end.setdefault("failure", {})
        self.s_combo("After a victory##end", victory, "transition", scn.TRANSITIONS_VICTORY, "Change victory ending")
        if victory.get("transition") == "next_scenario":
            others = sorted(k for k in project.scenarios if k != self.scenario_key)
            self.s_combo("Next scenario##end", victory, "next", [None] + others, "Change next scenario",
                         {None: "choose...", **{k: project.scenarios[k].get("title", k) for k in others}})
        self.s_text("Victory message##end", victory, "message", "Edit victory message")
        self.s_combo("After a failure##end", failure, "transition", scn.TRANSITIONS_FAILURE, "Change failure ending")
        self.s_text("Failure message##end", failure, "message", "Edit failure message")
        self.problems_for(problems, "start")
        self.problems_for(problems, "end")
        imgui.end_child()

    def condition_list(self, scenario: dict, family: str, problems) -> None:
        conditions = scenario.setdefault(family, [])
        allowed = [k for k in scn.CONDITIONS if not (family == "victory" and k in scn.FAILURE_ONLY) and not (family == "failure" and k in scn.VICTORY_ONLY)]
        for number, condition in enumerate(list(conditions)):
            imgui.push_id(f"{family}{number}")
            imgui.bullet()
            when = condition.get("when") if isinstance(condition, dict) else None
            imgui.set_next_item_width(220)
            if imgui.begin_combo("##when", str(when).replace("_", " ")):
                for option in allowed:
                    if imgui.selectable(option.replace("_", " "), option == when)[0]:
                        self.scenario_change("Change condition", lambda o=option, n=number: conditions.__setitem__(n, {"when": o}))
                imgui.end_combo()
            for param, kind in scn.CONDITIONS.get(when, {}).items():
                imgui.same_line()
                imgui.set_next_item_width(170)
                if kind in ("objective", "timer", "counter"):
                    self.element_combo(f"##{param}", condition, param, scenario, kind + "s", "Change condition")
                elif kind == "record":
                    self.record_combo(f"##{param}", condition.get(param), scn.DESTRUCTIBLE_CATEGORIES, lambda v, cd=condition, p=param: self.scenario_change(
                        "Change condition", lambda: cd.__setitem__(p, v)), map_key=scenario.get("map"))
                else:
                    self.s_int(f"##{param}", condition, param, -1_000_000, 1_000_000, "Change condition")
            imgui.same_line()
            if imgui.small_button("Remove"):
                self.scenario_change("Remove condition", lambda n=number: conditions.pop(n))
            imgui.pop_id()
        if imgui.small_button(f"Add {family} condition"):
            self.scenario_change("Add condition", lambda: conditions.append({"when": allowed[0]}))
        self.problems_for(problems, family)

    def scenario_tab_rules(self, scenario: dict, problems) -> None:
        rules = scenario.setdefault("rules", {})
        score = rules.setdefault("score", {})
        self.s_int("Teams", rules, "teams", 1, 8, "Change teams", 2)
        self.s_int("Time limit (seconds, 0 = none)", rules, "time_limit_seconds", 0, 7200, "Change time limit")
        self.s_int("Score limit (0 = none)", rules, "score_limit", 0, 1_000_000, "Change score limit")
        self.s_int("Lives (0 = unlimited)", rules, "lives", 0, 99, "Change lives", 3)
        self.s_bool("Friendly fire", rules, "friendly_fire", "Change friendly fire")
        imgui.separator_text("Score")
        self.s_int("Points per tank destroyed", score, "kill", -10000, 10000, "Change kill points", 100)
        self.s_int("Points per objective", score, "objective", -10000, 10000, "Change objective points", 500)
        multipliers = score.setdefault("multipliers", [])
        imgui.text("Score multipliers")
        for number, multiplier in enumerate(list(multipliers)):
            imgui.push_id(f"mult{number}")
            imgui.bullet()
            imgui.set_next_item_width(130)
            self.s_combo("##applies", multiplier, "applies", ("kills", "objectives", "all"), "Change multiplier")
            imgui.same_line()
            imgui.set_next_item_width(130)
            self.s_float("##factor", multiplier, "factor", 0.1, 10.0, "Change multiplier", 2.0)
            imgui.same_line()
            imgui.set_next_item_width(200)
            self.record_combo("while inside##zone", multiplier.get("zone"), scn.VOLUME_CATEGORIES, lambda v, m=multiplier: self.scenario_change(
                "Change multiplier zone", lambda: m.__setitem__("zone", v)), map_key=scenario.get("map"))
            imgui.same_line()
            if imgui.small_button("Remove"):
                self.scenario_change("Remove multiplier", lambda n=number: multipliers.pop(n))
            imgui.pop_id()
        if imgui.small_button("Add multiplier"):
            self.scenario_change("Add multiplier", lambda: multipliers.append({"id": scn.next_id("mul", {m.get("id") for m in multipliers}),
                                                                             "applies": "kills", "factor": 2.0, "zone": None}))
        self.problems_for(problems, "rules")

    def scenario_tab_map(self, scenario: dict, problems) -> None:
        c = self.controller
        if scenario.get("map") != c.active_map or c.document is None:
            imgui.text_disabled("Open this scenario's map to place and pair its objects.")
            return
        imgui.text("Place objects on this map (then click in the viewport):")
        for kind, label in PLACEABLE:
            if imgui.small_button(label):
                self.place_template(kind)
            imgui.same_line()
        imgui.new_line()
        imgui.separator_text("Teleporter pairs")
        pads = self.map_records(("teleporter",))
        if not pads:
            imgui.text_disabled("No teleporter entrances on this map.")
        for pad in pads:
            imgui.text(self.record_label(pad))
            imgui.same_line()
            imgui.text("->")
            imgui.same_line()
            imgui.set_next_item_width(260)
            current = (pad.get("fields", {}).get("exit") or {}).get("value")
            self.record_combo(f"##exit{pad['id']}", current, ("teleport_exit",),
                              lambda v, p=pad: self.attempt("Pair teleporter", c.document.set_field, p["id"], "exit", v), allow_none=True)
        imgui.separator_text("Objects scenarios can use")
        for category, noun in (("trigger_volume", "trigger volumes"), ("capture_zone", "zones"), ("destructible_target", "destructible targets"),
                               ("bot_slot", "bots"), ("item_pickup", "item pickups"), ("navigation_node", "navigation points")):
            records = self.map_records((category,))
            if imgui.tree_node(f"{len(records)} {noun}##obj{category}"):
                for record in records:
                    if imgui.selectable(f"{self.record_label(record)}##sel{record['id']}", record["id"] in c.document.selection)[0]:
                        c.frame_record(record["id"])
                imgui.tree_pop()

    def place_template(self, kind: str, values: dict | None = None) -> None:
        c = self.controller
        template = next((t["id"] for t in (c.library.starter() or {}).get("templates", []) if t["source_kind"] == f"studio_{kind}"), None)
        if template is None:
            self.report_error("This object is not available", "The Studio starter templates could not be found.")
            return
        try:
            c.begin_placement("marker", template, values)
            c.log("info", "click in the viewport to place; Esc cancels")
        except EditError as error:
            c.log("error", str(error))

    def scenario_tab_check(self, scenario: dict, problems) -> None:
        if not problems:
            imgui.text_colored(_vec(GREEN), "Studio found no problems in this scenario.")
        for problem in problems:
            imgui.text_colored(_vec(RED if problem.severity == "error" else AMBER), f"{problem.code}")
            imgui.same_line()
            imgui.text_wrapped(f"{problem.field or ''}: {problem.message}")
        imgui.separator_text("What the game must support to play it")
        capabilities = self.controller.runtime_capabilities()
        declared = set((capabilities or {}).get("features") or [])
        imgui.text_wrapped("Studio supports authoring every feature below. Whether a game build plays it is decided by the PC runtime, "
                           "which declares its features; none is assumed.")
        for feature in scn.required_features(scenario):
            state = ("declared by the runtime" if feature in declared else "not declared by the runtime") if capabilities else "runtime not configured"
            imgui.text_colored(_vec(GREEN if feature in declared else AMBER), f"{feature}")
            imgui.same_line()
            imgui.text_disabled(f"- {runtime_module.FEATURES.get(feature, '')} ({state})")

    # -- items ---------------------------------------------------------------------------------
    def window_items(self) -> None:
        self.bring_to_front("items", self.show_items)
        if not self.show_items:
            return
        c = self.controller
        imgui.set_next_window_size(imgui.ImVec2(1000, 600), imgui.Cond_.appearing)
        imgui.set_next_window_pos(imgui.ImVec2(120, 80), imgui.Cond_.appearing)
        imgui.set_next_window_bg_alpha(1.0)
        opened, self.show_items = imgui.begin("Items and pickups###items", self.show_items, imgui.WindowFlags_.no_collapse)
        if opened:
            if c.project is None:
                imgui.text_disabled("Open or create a project to define items.")
            else:
                self.items_body()
        imgui.end()

    def item_change(self, label: str, mutate, coalesce: str | None = None) -> bool:
        changed = self.change(label, mutate, coalesce, {"tab": "Items", "window": "items", "key": self.item_key})
        if changed:
            self.controller.request_diagnostics()
        return changed

    def items_body(self) -> None:
        c = self.controller
        project = c.project
        known_models = set(c.library._models) if c.library is not None else None
        problems = items_module.check_items(project.items, known_models, set(c.library._textures) if c.library else None)
        imgui.begin_child("item-list", imgui.ImVec2(240, 0), imgui.ChildFlags_.borders)
        self.history_buttons()
        imgui.separator()
        for key, item in sorted(project.items.items()):
            bad = any(p.target == f"items:{key}" and p.severity == "error" for p in problems)
            label = f"{item.get('title', key)}  ({item.get('category')})"
            if bad:
                imgui.push_style_color(imgui.Col_.text, _vec(RED))
            if imgui.selectable(f"{label}##it{key}", key == self.item_key)[0]:
                self.item_key = key
            if bad:
                imgui.pop_style_color()
        imgui.separator()
        if imgui.begin_combo("##newitem", "New item from..."):
            for preset, data in items_module.PRESETS.items():
                if imgui.selectable(f"{data['title']} ({data['category']})##{preset}", False)[0]:
                    key = items_module.item_key(data["title"], project.items)
                    if self.item_change("New item", lambda k=key, p=preset: project.define("items", k, items_module.new_item(p))):
                        self.item_key = key
            imgui.end_combo()
        imgui.end_child()
        imgui.same_line()
        imgui.begin_child("item-editor", imgui.ImVec2(0, 0), imgui.ChildFlags_.borders)
        item = project.items.get(self.item_key)
        if item is None:
            imgui.text_wrapped("Items are defined once and can be placed on every map of the project as pickups. Their effects are a "
                               "list of actions (no code): change health, armor, ammunition, currency or score, give or take a weapon "
                               "or item, change a stat for a while, add to a counter, fire a trigger, complete an objective, show a "
                               "message or play a sound.")
        else:
            self.item_editor(self.item_key, item, problems)
        imgui.end_child()

    def item_editor(self, key: str, item: dict, problems) -> None:
        c = self.controller

        def text(label, name, limit):
            changed, value = imgui.input_text(label, str(item.get(name, "") or ""))
            if changed:
                self.item_change("Edit item", lambda: item.__setitem__(name, value[:limit]), f"text:item{key}{name}")
            self.seal_if_done()

        def number(label, name, lo, hi, default=0):
            changed, value = imgui.input_int(label, int(item.get(name, default) or 0))
            if changed:
                self.item_change("Edit item", lambda: item.__setitem__(name, max(lo, min(hi, int(value)))), f"int:item{key}{name}")
            self.seal_if_done()

        def combo(label, name, options):
            current = item.get(name)
            if imgui.begin_combo(label, str(current).replace("_", " ")):
                for option in options:
                    if imgui.selectable(option.replace("_", " "), option == current)[0]:
                        self.item_change("Edit item", lambda o=option: item.__setitem__(name, o))
                imgui.end_combo()

        imgui.text(f"Item {key}")
        text("Name##item", "title", 48)
        combo("Category##item", "category", items_module.CATEGORIES)
        self.model_picker("Model##item", item.get("model"), {"crate", "pad", "prop"}, lambda v: self.item_change(
            "Change item model", lambda: item.__setitem__("model", v or None)))
        icon = str(item.get("icon") or "")
        if imgui.begin_combo("Icon##item", icon.rsplit(":", 1)[-1] or "none"):
            for name in items_module.ICONS:
                if imgui.selectable(name, icon == f"studio:icon:{name}")[0]:
                    self.item_change("Change item icon", lambda n=name: item.__setitem__("icon", f"studio:icon:{n}"))
            imgui.end_combo()
        number("Duration (seconds, 0 = instant)##item", "duration_seconds", 0, 3600)
        combo("Stacking##item", "stacking", items_module.STACKING)
        number("Max stack##item", "max_stack", 1, 99, 1)
        imgui.separator_text("Effects")
        self.action_list(f"Effects##{key}", item, "effects", "item", None,
                         lambda label, mutate, coalesce=None: self.item_change(label, mutate, coalesce))
        imgui.separator()
        imgui.text_wrapped(items_module.summary(item, self._element_names(None)))
        for problem in problems:
            if problem.target == f"items:{key}":
                imgui.text_colored(_vec(RED if problem.severity == "error" else AMBER), f"{problem.code}: {problem.field}: {problem.message}")
        placed = [r for r in self.map_records(("item_pickup",)) if bots_module.value(r, "item") == key]
        imgui.text(f"{len(placed)} pickup(s) of this item on the open map")
        imgui.begin_disabled(c.document is None)
        if imgui.button("Place a pickup of this item"):
            self.place_template("item_pickup", {"item": key})
        imgui.end_disabled()
        imgui.same_line()
        if imgui.button("Delete item"):
            self.confirm("Delete item", f"Delete \"{item.get('title', key)}\"? Pickups that give it will report an error. You can undo this.",
                         "Delete", lambda: self.item_change("Delete item", lambda: c.project.items.pop(key)))

    # -- inspector additions ---------------------------------------------------------------------
    SPECIAL_FIELDS = {"bot_slot": {"kind", "model", "role", "patrol_start", "guard_target", "capture_target", "escort_target", "team",
                                   "allowed_weapons"},
                      "item_pickup": {"item"}, "destructible_target": {"model", "destroyed_model"}}

    def authoring_inspector(self, record: dict) -> set[str]:
        """Friendly editors for bots, item pickups and destructible targets; returns the field names they handled."""

        category = record.get("category")
        if category not in self.SPECIAL_FIELDS or record.get("disabled"):
            return set()
        c = self.controller
        doc = c.document
        rid = record["id"]

        def set_field(name, value):
            try:
                doc.set_field(rid, name, value)
                self.field_error = None
                c.request_diagnostics()
            except EditError as error:
                self.field_error = f"{name}: {error}"

        if category == "bot_slot":
            imgui.separator_text("Bot")
            kind = bots_module.value(record, "kind", 0)
            for number, label in enumerate(("Tank", "Queenlord")):
                if number:
                    imgui.same_line()
                if imgui.radio_button(f"{label}##kind{rid}", kind == number) and kind != number:
                    set_field("kind", number)
            roles = {"queenlord"} if kind == 1 else {"tank"}
            self.model_picker(f"Model##bot{rid}", bots_module.value(record, "model"), roles, lambda v: set_field("model", v))
            model = bots_module.value(record, "model")
            if kind == 1 and not model:
                imgui.text_colored(_vec(RED), "No Queenlord model chosen: this Queenlord stays unresolved and blocks export.")
            elif model in game_models.BY_ID and game_models.BY_ID[model]["evidence"] != "proven":
                imgui.text_colored(_vec(AMBER), f"Model identification: {game_models.BY_ID[model]['evidence']}")
            team = bots_module.value(record, "team", 0)
            if imgui.begin_combo(f"Team##bot{rid}", f"team {team}"):
                for option in range(8):
                    if imgui.selectable(f"team {option}", option == team)[0]:
                        set_field("team", option)
                imgui.end_combo()
            role = bots_module.value(record, "role", 0)
            allowed = [r for r in range(len(bots_module.ROLES)) if kind != 1 or bots_module.ROLES[r] in bots_module.QUEENLORD_ROLES]
            if imgui.begin_combo(f"Behaviour##bot{rid}", bots_module.role_name(record)):
                for option in allowed:
                    if imgui.selectable(bots_module.ROLES[option], option == role)[0]:
                        set_field("role", option)
                imgui.end_combo()
            role_name = bots_module.role_name(record)
            targets = {"patrol": [("patrol_start", "Patrol starts at", ("navigation_node",))],
                       "defend": [("guard_target", "Defends", bots_module.GUARD_CATEGORIES)],
                       "capture": [("capture_target", "Captures", ("capture_zone",))],
                       "escort": [("escort_target", "Escorts", ("bot_slot",) + bots_module.GUARD_CATEGORIES)],
                       "support": [("escort_target", "Supports", ("bot_slot",))]}.get(role_name, [])
            for name, label, categories in targets:
                self.record_combo(f"{label}##{rid}{name}", bots_module.value(record, name), categories, lambda v, n=name: set_field(n, v))
            weapons = bots_module.value(record, "allowed_weapons", 0) or 0
            imgui.text("Weapons it may use:")
            for bit, weapon in enumerate(act.WEAPONS):
                if bit % 3:
                    imgui.same_line()
                changed, on = imgui.checkbox(f"{weapon.replace('_', ' ')}##w{rid}{bit}", bool(weapons >> bit & 1))
                if changed:
                    set_field("allowed_weapons", weapons ^ (1 << bit))
            if not targets:
                imgui.text_disabled({"attack": "Attacks the nearest enemies (the runtime decides how).",
                                     "boss": "A boss: pair it with a 'Defeat a boss' objective in a scenario."}.get(role_name, ""))
            imgui.text_disabled("How a bot behaves in play is decided by the PC runtime; Studio records the setup only.")
            mine = [p for p in bots_module.check_bots(doc, c.library, c.runtime_capabilities()) if p.target == rid]
            for problem in mine:
                imgui.text_colored(_vec(RED), f"{problem.code}: {problem.message}")
        elif category == "item_pickup":
            imgui.separator_text("Item pickup")
            items = c.project.items if c.project else {}
            current = bots_module.value(record, "item")
            preview = items[current].get("title", current) if current in items else ("choose an item" if not current else f"missing item {current}")
            if imgui.begin_combo(f"Gives##{rid}", preview):
                for key, item in sorted(items.items()):
                    if imgui.selectable(f"{item.get('title', key)} ({item.get('category')})##{key}", key == current)[0]:
                        set_field("item", key)
                if not items:
                    imgui.text_disabled("No items defined yet")
                imgui.end_combo()
            if imgui.small_button(f"Edit items...##{rid}"):
                self.show_items = True
                self.item_key = current if current in items else self.item_key
        elif category == "destructible_target":
            imgui.separator_text("Destructible target")
            self.model_picker(f"Intact model##{rid}", bots_module.value(record, "model"), {"destructible", "base", "crate", "prop", "pad", "barrier"},
                              lambda v: set_field("model", v))
            self.model_picker(f"Wreck model##{rid}", bots_module.value(record, "destroyed_model"), {"destructible_wreck", "base_damaged", "prop"},
                              lambda v: set_field("destroyed_model", v))
        return self.SPECIAL_FIELDS[category]

    GAME_RECORD_MODELS = ("model_0", "structure_offset.model_0")

    def marker_models(self, record: dict) -> list[str]:
        """Models drawn at a bot, destructible target or item pickup, and at original game records that create an object from
        their first model (renderer callback).  For game records the placement (the record's position and facing) is inferred."""

        category = record.get("category")
        if not record.get("overlay") and record.get("platform") == "n64_btga" and self.settings.preferences.layers.get("record_models", True):
            for name in self.GAME_RECORD_MODELS:
                for dependency in record.get("dependencies", []):
                    if dependency.get("field") == name and dependency.get("kind") == "model" and dependency.get("id"):
                        return [dependency["id"]]
            return []
        if category in ("bot_slot", "destructible_target"):
            return game_models.parts(bots_module.value(record, "model"))
        if category == "item_pickup" and self.controller.project is not None:
            item = self.controller.project.items.get(bots_module.value(record, "item")) or {}
            return [item["model"]] if item.get("model") else []
        return []

    # -- Test in Game ------------------------------------------------------------------------------
    def runtime_state(self) -> tuple[dict | None, str | None]:
        """(capabilities, problem) for the configured runtime, re-read when its sidecar changes."""

        path = self.settings.preferences.runtime_executable
        sidecar = Path(path).with_name(runtime_module.SIDECAR) if path else None
        try:
            stamp = (Path(path).stat().st_mtime_ns, sidecar.stat().st_mtime_ns) if path and sidecar.is_file() and Path(path).is_file() else None
        except OSError:
            stamp = None
        cached_path, cached_stamp, result = self._runtime_cache
        if cached_path == path and cached_stamp == stamp and result is not None:
            return result
        try:
            result = (runtime_module.read_capabilities(path), None)
        except runtime_module.RuntimeProblem as error:
            result = (None, f"{error.code}: {error}")
        self._runtime_cache = (path, stamp, result)
        return result

    def test_in_game_button(self) -> None:
        capabilities, problem = self.runtime_state()
        imgui.begin_disabled(capabilities is None or self.controller.project is None)
        if imgui.button("Test in Game"):
            self.open_test_in_game()
        imgui.end_disabled()
        self.anchor("toolbar.test_in_game")
        if imgui.is_item_hovered(imgui.HoveredFlags_.allow_when_disabled):
            if capabilities is None:
                imgui.set_tooltip(f"Unavailable: {problem}\nConfigure a compatible PC runtime in Settings > Test in Game.")
            else:
                runtime = capabilities["runtime"]
                imgui.set_tooltip(f"Launch {runtime['name']} {runtime['version']} with this project's exported package")

    def open_test_in_game(self) -> None:
        c = self.controller
        self.show_test_in_game = True
        if self.launch["map"] not in (c.project.maps if c.project else {}):
            self.launch["map"] = c.active_map
        self.launch_plan = None
        self.launch_status = None

    def check_launch(self) -> runtime_module.LaunchPlan:
        c = self.controller
        prefs = self.settings.preferences
        launch = self.launch
        plan = runtime_module.preflight(c.project, c.last_export, prefs.runtime_executable, prefs.runtime_rom, launch["map"], launch["scenario"],
                                        launch["spawn"], sorted(launch["bots"]), launch["capture"], self._capture_directory(),
                                        c.open_documents(), getattr(self, "studio_version", "0"))
        self.launch_plan = plan
        self.launch_checked = time.monotonic()
        return plan

    def _capture_directory(self):
        if self.launch["capture"] == "none":
            return None
        return self.settings.local_directory / "captures"

    def window_test_in_game(self) -> None:
        self.bring_to_front("test-in-game", self.show_test_in_game)
        if not self.show_test_in_game:
            return
        c = self.controller
        imgui.set_next_window_size(imgui.ImVec2(620, 0), imgui.Cond_.appearing)
        imgui.set_next_window_bg_alpha(1.0)
        opened, self.show_test_in_game = imgui.begin("Test in Game###test-in-game", self.show_test_in_game,
                                                     imgui.WindowFlags_.no_collapse | imgui.WindowFlags_.always_auto_resize)
        if opened and c.project is not None:
            capabilities, problem = self.runtime_state()
            if capabilities is None:
                imgui.text_colored(_vec(RED), f"No compatible PC runtime: {problem}")
            else:
                runtime = capabilities["runtime"]
                imgui.text_colored(_vec(GREEN), f"Runtime: {runtime['name']} {runtime['version']} (declares {len(capabilities.get('features') or [])} features)")
            imgui.text_wrapped("Starts the real game runtime with your exported package. Studio does not simulate anything here and "
                               "never falls back to Drive Preview.")
            launch = self.launch
            project = c.project
            maps = sorted(project.maps)
            if imgui.begin_combo("Map##launch", project.maps[launch["map"]].title if launch["map"] in project.maps else "choose a map"):
                for key in maps:
                    if imgui.selectable(project.maps[key].title, key == launch["map"])[0]:
                        launch.update({"map": key, "scenario": None, "spawn": None, "bots": set()})
                        if key != c.active_map:
                            self.switch_map(key)
                imgui.end_combo()
            scenarios = {k: v for k, v in project.scenarios.items() if isinstance(v, dict) and v.get("map") == launch["map"]}
            if imgui.begin_combo("Scenario##launch", scenarios[launch["scenario"]].get("title") if launch["scenario"] in scenarios else "none (free play)"):
                if imgui.selectable("none (free play)", launch["scenario"] is None)[0]:
                    launch["scenario"] = None
                for key, scenario in sorted(scenarios.items()):
                    if imgui.selectable(scenario.get("title", key), key == launch["scenario"])[0]:
                        launch["scenario"] = key
                        launch["spawn"] = (scenario.get("start") or {}).get("player_spawn")
                imgui.end_combo()
            self.record_combo("Player spawn##launch", launch["spawn"], scn.SPAWN_CATEGORIES, lambda v: launch.__setitem__("spawn", v),
                              map_key=launch["map"])
            bots = self.map_records(("bot_slot",), launch["map"])
            if bots:
                imgui.text("Bot slots to request:")
                for bot in bots:
                    slot = bots_module.value(bot, "ai_slot")
                    changed, on = imgui.checkbox(f"{self.record_label(bot)}##launchbot{bot['id']}", slot in launch["bots"])
                    if changed:
                        (launch["bots"].add if on else launch["bots"].discard)(slot)
            modes = (capabilities or {}).get("capture_modes") or ["none"]
            if imgui.begin_combo("Capture##launch", launch["capture"]):
                for mode in runtime_module.CAPTURE_MODES:
                    if imgui.selectable(f"{mode}{'' if mode in modes else ' (not offered by the runtime)'}", mode == launch["capture"])[0]:
                        launch["capture"] = mode
                imgui.end_combo()
            export = c.last_export
            imgui.text(f"Package: {Path(export.path).name} ({export.size / 1e6:.1f} MB)" if export else "Package: not exported in this session")
            imgui.same_line()
            if imgui.small_button("Export now..."):
                self.export_package()
            imgui.separator()
            if imgui.button("Check"):
                self.check_launch()
            plan = self.launch_plan
            if plan is not None:
                if plan.problems:
                    for code, message in plan.problems:
                        imgui.text_colored(_vec(RED), code)
                        imgui.same_line()
                        imgui.text_wrapped(message)
                else:
                    imgui.text_colored(_vec(GREEN), "Ready: every check passed.")
            imgui.same_line()
            imgui.begin_disabled(plan is None or not plan.ok)
            if imgui.button("Launch"):
                self.start_runtime(plan)
            imgui.end_disabled()
            if self.launch_status:
                imgui.text_wrapped(self.launch_status)
        imgui.end()

    def start_runtime(self, plan: runtime_module.LaunchPlan) -> None:
        c = self.controller
        try:
            runtime_module.launch(plan, self.settings.preferences.runtime_executable)
        except (runtime_module.RuntimeProblem, OSError) as error:
            self.launch_status = f"The game was not started: {error}"
            c.log("error", self.launch_status)
            return
        self.launch_status = f"Started session {plan.request['session_id'][:8]} with {Path(plan.request['package']['path']).name}."
        c.log("info", f"Test in Game: runtime started, session {plan.request['session_id']}")

    def settings_runtime_section(self) -> None:
        prefs = self.settings.preferences
        imgui.separator_text("Test in Game")
        imgui.text_wrapped("Choose the PC runtime program and your Global Assault cartridge file. Both stay on this computer: they are "
                           "never saved into projects or packages. The cartridge path is only handed to the runtime when you launch.")
        for label, attribute, title in (("Runtime program", "runtime_executable", "Choose the PC runtime program"),
                                        ("Cartridge file", "runtime_rom", "Choose your Global Assault cartridge")):
            value = getattr(prefs, attribute) or ""
            imgui.text(f"{label}: {Path(value).name if value else 'not set'}")
            if value and imgui.is_item_hovered():
                imgui.set_tooltip(value)
            imgui.same_line()
            if imgui.small_button(f"Choose...##{attribute}"):
                chosen = self.dialogs.open_file(title, ["All files", "*"])
                if chosen:
                    setattr(prefs, attribute, str(chosen))
                    self._runtime_cache = (None, None, None)
            if value:
                imgui.same_line()
                if imgui.small_button(f"Clear##{attribute}"):
                    setattr(prefs, attribute, None)
                    self._runtime_cache = (None, None, None)
        capabilities, problem = self.runtime_state()
        if capabilities is not None:
            imgui.text_colored(_vec(GREEN), f"Compatible: {capabilities['runtime']['name']} {capabilities['runtime']['version']}")
        else:
            imgui.text_colored(_vec(AMBER), f"Test in Game unavailable: {problem}")
