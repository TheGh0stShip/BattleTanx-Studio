"""Scenarios, items, bots, the runtime launch contract and their package representation (synthetic, no game data)."""

from __future__ import annotations

import copy
import hashlib
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tests.test_studio_authoring import _egl_available, example_project, starter_library  # noqa: E402
from tools.btga_studio.core import actions as act  # noqa: E402
from tools.btga_studio.core import authoring  # noqa: E402
from tools.btga_studio.core import bots  # noqa: E402
from tools.btga_studio.core import items  # noqa: E402
from tools.btga_studio.core import package  # noqa: E402
from tools.btga_studio.core import project as projects  # noqa: E402
from tools.btga_studio.core import runtime  # noqa: E402
from tools.btga_studio.core import scenario as scn  # noqa: E402


def codes(problems) -> set[str]:
    return {p.code for p in problems}


class Fixture:
    """The example project with its training-grounds map open, plus helpers to look up records."""

    def __init__(self):
        self.library = starter_library()
        self.project = example_project()
        self.doc = self.project.open_map("training_grounds", self.library)
        self.templates = {t["source_kind"].removeprefix("studio_"): t for t in self.library.starter()["templates"]}
        self.models = self.library.starter()["models"]

    def ids(self, category) -> list[str]:
        return [r["id"] for _, r in self.doc.scene.records("markers") if r.get("category") == category and not r.get("disabled")]

    def ctx(self):
        return scn.context_for(self.project, "first_strike", self.doc, self.library)

    def check(self, scenario=None):
        return scn.check("first_strike", scenario if scenario is not None else self.project.scenarios["first_strike"], self.ctx())


class ScenarioTests(unittest.TestCase):
    def setUp(self):
        self.f = Fixture()
        self.scenario = copy.deepcopy(self.f.project.scenarios["first_strike"])

    def test_example_scenario_is_valid_and_lists_its_runtime_features(self):
        self.assertEqual(self.f.check(), [])
        features = scn.required_features(self.scenario)
        for feature in ("scenario", "objective.capture", "objective.flow.requires", "objective.flow.optional", "trigger.enter",
                        "action.start_timer", "scenario.waves", "transition.start.briefing"):
            self.assertIn(feature, features)
        self.assertTrue(set(features) <= set(runtime.FEATURES))  # every requirement is explained

    def test_dangling_references_are_reported_with_their_field(self):
        objective = self.scenario["objectives"]["obj1"]
        objective["params"]["area"] = "user:nowhere:1"
        objective["requires"] = ["obj99"]
        self.scenario["triggers"]["trg1"]["actions"].append({"do": "start_timer", "timer": "tmr9"})
        problems = self.f.check(self.scenario)
        fields = {(p.code, p.field) for p in problems}
        self.assertIn(("DANGLING_REFERENCE", "objectives.obj1.params.area"), fields)
        self.assertIn(("DANGLING_REFERENCE", "objectives.obj1.requires"), fields)
        self.assertIn(("DANGLING_REFERENCE", "triggers.trg1.actions[2].timer"), fields)

    def test_wrong_record_kind_and_disabled_records(self):
        self.scenario["objectives"]["obj2"]["params"]["zone"] = self.f.ids("trigger_volume")[0]  # capture needs a capture zone
        self.assertIn("INVALID_REFERENCE", codes(self.f.check(self.scenario)))
        self.f.doc.delete([self.f.ids("capture_zone")[0]])
        scenario = self.f.project.scenarios["first_strike"]
        self.assertIn("DANGLING_REFERENCE", codes(self.f.check(scenario)))

    def test_requirement_cycles_are_forbidden(self):
        self.scenario["objectives"]["obj1"]["requires"] = ["obj3"]  # obj3 -> obj2 -> obj1 -> obj3
        problems = self.f.check(self.scenario)
        cycle = [p for p in problems if p.code == "OBJECTIVE_CYCLE"]
        self.assertEqual(len(cycle), 1)
        self.assertIn("->", cycle[0].message)

    def test_trigger_loops_are_forbidden(self):
        first = scn.add_element(self.scenario, "triggers", "A")
        second = scn.add_element(self.scenario, "triggers", "B")
        for a, b in ((first, second), (second, first)):
            self.scenario["triggers"][a].update({"on": "scenario_start", "actions": [{"do": "activate_trigger", "trigger": b}]})
        self.assertIn("TRIGGER_CYCLE", codes(self.f.check(self.scenario)))

    def test_impossible_objectives(self):
        cases = []
        destroy = copy.deepcopy(self.scenario)
        destroy["objectives"]["obj3"]["params"]["count"] = 5  # one target listed
        cases.append(destroy)
        survive = copy.deepcopy(self.scenario)
        survive["rules"]["time_limit_seconds"] = 60
        element = scn.add_element(survive, "objectives", "survive")
        survive["objectives"][element]["params"]["seconds"] = 120
        cases.append(survive)
        race = copy.deepcopy(self.scenario)
        element = scn.add_element(race, "objectives", "race")
        race["objectives"][element]["params"]["checkpoints"] = self.f.ids("trigger_volume")
        cases.append(race)
        speed = next(r for r in self.f.ids("item_pickup") if self.f.doc.record(r)["fields"]["item"]["value"] == "speed_boost")
        self.f.doc.set_field(speed, "respawn_seconds", 0)  # one pickup that never comes back gives one
        collect = copy.deepcopy(self.scenario)
        element = scn.add_element(collect, "objectives", "collect")
        collect["objectives"][element]["params"].update({"item": "speed_boost", "count": 5})
        cases.append(collect)
        forever = copy.deepcopy(self.scenario)
        forever["objectives"]["obj1"]["repeat"] = 0
        cases.append(forever)  # obj2 requires obj1, which never finishes
        for scenario in cases:
            self.assertIn("IMPOSSIBLE_OBJECTIVE", codes(self.f.check(scenario)), json.dumps(scenario["objectives"])[:200])

    def test_an_objective_that_needs_an_impossible_one_is_impossible_too(self):
        self.scenario["rules"]["time_limit_seconds"] = 10  # holding the hill takes 15 s, so it can never be done
        problems = self.f.check(self.scenario)
        self.assertIn(("IMPOSSIBLE_OBJECTIVE", "objectives.obj2.params.hold_seconds"), {(p.code, p.field) for p in problems})
        self.assertIn(("IMPOSSIBLE_OBJECTIVE", "objectives.obj3.requires"), {(p.code, p.field) for p in problems})  # needs obj2

    def test_victory_must_be_possible(self):
        self.scenario["victory"] = []
        self.assertIn("NO_VICTORY", codes(self.f.check(self.scenario)))
        for objective in self.scenario["objectives"].values():
            objective["optional"] = True
        self.scenario["victory"] = [{"when": "all_required_objectives"}]
        self.assertIn("IMPOSSIBLE_CONDITION", codes(self.f.check(self.scenario)))
        self.scenario["victory"] = [{"when": "player_destroyed"}]
        self.assertIn("INVALID_FIELD", codes(self.f.check(self.scenario)))  # a failure-only condition

    def test_unsupported_features_and_actions_fail_validation(self):
        self.scenario["objectives"]["obj1"]["kind"] = "teleport_everyone"
        self.scenario["triggers"]["trg1"]["actions"].append({"do": "run_script", "code": "print(1)"})
        self.scenario["triggers"]["trg1"]["on"] = "when_it_rains"
        found = {p.code for p in self.f.check(self.scenario) if p.severity == "error"}
        self.assertTrue({"UNSUPPORTED_OBJECTIVE", "UNSUPPORTED_TRIGGER"} <= found, found)
        self.scenario["triggers"]["trg1"]["on"] = "enter"
        self.assertIn("UNSUPPORTED_ACTION", codes(self.f.check(self.scenario)))

    def test_unreachable_objective_area(self):
        doc = self.f.doc
        far = doc.place_marker(self.f.templates["capture_zone"], (900.0, 0.0, 900.0), values={"label": "Island"})
        island = doc.place_marker(self.f.templates["nav_node"], (900.0, 0.0, 900.0))
        self.assertTrue(island)
        self.scenario["objectives"]["obj2"]["params"]["zone"] = far
        self.assertIn("UNREACHABLE_OBJECTIVE", codes(self.f.check(self.scenario)))

    def test_ids_are_deterministic_and_removal_cleans_references(self):
        a, b = scn.new_scenario("A", "m"), scn.new_scenario("A", "m")
        for scenario in (a, b):
            for kind in ("reach", "destroy", "capture"):
                scn.add_element(scenario, "objectives", kind)
        self.assertEqual(a, b)
        self.assertEqual(sorted(a["objectives"]), ["obj1", "obj2", "obj3"])
        a["objectives"]["obj3"]["requires"] = ["obj2"]
        scn.remove_element(a, "objectives", "obj2")
        self.assertEqual(a["objectives"]["obj3"]["requires"], [])
        self.assertEqual(scn.add_element(a, "objectives", "reach"), "obj2")  # the smallest free ID is reused

    def test_unknown_fields_survive_save_and_reload(self):
        project = self.f.project
        project.scenarios["first_strike"]["from_a_newer_studio"] = {"keep": [1, 2]}
        project.scenarios["first_strike"]["objectives"]["obj1"]["glow"] = "red"
        project.items["repair_kit"]["particle"] = "sparks"
        text = projects.dumps(project)
        again = projects.loads_project(text)
        self.assertEqual(again.scenarios["first_strike"]["from_a_newer_studio"], {"keep": [1, 2]})
        self.assertEqual(again.scenarios["first_strike"]["objectives"]["obj1"]["glow"], "red")
        self.assertEqual(again.items["repair_kit"]["particle"], "sparks")
        self.assertEqual(projects.dumps(again), text)  # deterministic

    def test_migration_from_project_version_2(self):
        data = json.loads(projects.dumps(self.f.project))
        data["schema_version"] = 2
        data.pop("scenarios")
        data.pop("items")
        data["missions"]["first_strike"].pop("scenario")
        migrated = projects.StudioProject.from_dict(data)
        scenario = migrated.scenarios["first_strike"]
        self.assertEqual(scenario["map"], "training_grounds")
        self.assertEqual(scenario["migrated_from"], {"family": "missions", "key": "first_strike"})
        self.assertEqual([o["params"]["marker"] for o in scenario["objectives"].values()], data["missions"]["first_strike"]["objectives"])
        self.assertEqual(migrated.missions["first_strike"]["scenario"], "first_strike")
        self.assertEqual(migrated.items, {})
        self.assertEqual(json.loads(projects.dumps(migrated))["schema_version"], 3)
        with self.assertRaises(projects.FutureVersion):
            projects.StudioProject.from_dict({**data, "schema_version": 4})

    def test_one_editor_action_is_one_undo_transaction(self):
        project = self.f.project
        history = project.definitions
        before = copy.deepcopy(project.scenarios)

        def add_and_link():  # what the graph editor's "Add objective" does in one user action
            element = scn.add_element(project.scenarios["first_strike"], "objectives", "survive")
            project.scenarios["first_strike"]["objectives"][element]["requires"] = ["obj3"]

        self.assertTrue(history.edit("Add objective", add_and_link))
        self.assertEqual(len(history.undo_stack), 1)
        history.undo()
        self.assertEqual(project.scenarios, before)
        history.redo()
        self.assertIn("obj5", project.scenarios["first_strike"]["objectives"])
        with self.assertRaises(KeyError):  # an invalid action leaves no trace
            history.edit("Broken", lambda: scn.add_element(project.scenarios["first_strike"], "objectives", "not_a_kind"))
        self.assertEqual(len(history.undo_stack), 1)


class ItemTests(unittest.TestCase):
    def setUp(self):
        self.library = starter_library()

    def test_presets_are_valid(self):
        catalog = {key: items.new_item(key) for key in items.PRESETS}
        self.assertEqual([p for p in items.check_items(catalog, set(self.library._models)) if p.severity == "error"], [])

    def test_ranges_assets_and_unsupported_effects(self):
        item = items.new_item("speed_boost")
        item.update({"duration_seconds": 99999, "stacking": "pile", "max_stack": 0, "model": "n64_btga:model:000000000000000000000000",
                     "icon": "studio:icon:rainbow"})
        item["effects"] += [{"do": "end_scenario", "result": "victory"}, {"do": "modify_stat", "stat": "health", "amount": 10**9, "mode": "add"},
                            {"do": "exec", "code": "import os"}]
        found = codes(items.check_item("boost", item, {"boost": item}, set(self.library._models)))
        self.assertTrue({"INVALID_FIELD", "MISSING_MODEL", "MISSING_ICON", "UNSUPPORTED_EFFECT"} <= found, found)

    def test_items_that_give_each_other_in_a_loop(self):
        a, b, c = items.new_item("keycard"), items.new_item("keycard"), items.new_item("keycard")
        a["effects"] = [{"do": "grant_item", "item": "b", "count": 1}]
        b["effects"] = [{"do": "grant_item", "item": "c", "count": 1}]
        c["effects"] = [{"do": "grant_item", "item": "a", "count": 1}]
        problems = items.check_items({"a": a, "b": b, "c": c})
        self.assertIn("ITEM_CYCLE", codes(problems))
        a["effects"] = [{"do": "grant_item", "item": "a", "count": 1}]
        self.assertIn("ITEM_CYCLE", codes(items.check_item("a", a, {"a": a})))

    def test_pickups_check_their_item_and_the_names_its_effects_use(self):
        f = Fixture()
        pickup = f.ids("item_pickup")[0]
        f.doc.set_field(pickup, "item", "nothing_here")
        problems = authoring.content_problems(f.project, f.library, {"training_grounds": f.doc})
        self.assertIn(("DANGLING_REFERENCE", pickup), {(p.code, p.target) for p in problems})
        f.doc.undo()
        f.project.items["repair_kit"]["effects"].append({"do": "activate_trigger", "trigger": "trg_missing"})
        problems = authoring.content_problems(f.project, f.library, {"training_grounds": f.doc})
        self.assertTrue(any(p.code == "DANGLING_REFERENCE" and p.field and p.field.startswith("item.effects") for p in problems))

    def test_item_keys_are_stable(self):
        self.assertEqual(items.item_key("Repair kit", {}), "repair_kit")
        self.assertEqual(items.item_key("Repair kit", {"repair_kit"}), "repair_kit_1")
        self.assertEqual(items.item_key("9 lives", {}), "item_9_lives")


class BotTests(unittest.TestCase):
    def setUp(self):
        self.f = Fixture()
        self.bots = self.f.ids("bot_slot")

    def check(self, profile=None):
        return bots.check_bots(self.f.doc, self.f.library, profile)

    def test_example_bots_are_valid_and_slots_are_claimed_once(self):
        self.assertEqual(self.check(), [])
        self.f.doc.set_field(self.bots[1], "ai_slot", 0)
        duplicate = [p for p in self.check() if p.code == "DUPLICATE_SLOT"]
        self.assertEqual({p.target for p in duplicate}, set(self.bots))

    def test_runtime_slot_and_team_limits(self):
        self.f.doc.set_field(self.bots[1], "ai_slot", 9)
        self.assertIn("BOT_SLOT_UNAVAILABLE", codes(self.check({"bot_slots": 4, "teams": 2})))
        self.f.doc.set_field(self.bots[1], "team", 3)
        self.assertIn("INVALID_TEAM", codes(self.check({"bot_slots": 16, "teams": 2})))

    def test_models_patrols_targets_and_reachability(self):
        doc, templates = self.f.doc, self.f.templates
        missing = doc.place_marker(templates["bot"], (300.0, 0.0, 300.0), values={"ai_slot": 5, "model": ""})
        unknown = doc.place_marker(templates["bot"], (320.0, 0.0, 300.0), values={"ai_slot": 6, "model": "n64_btga:model:" + "0" * 24})
        queen = doc.place_marker(templates["bot"], (340.0, 0.0, 300.0), values={"ai_slot": 7, "kind": 1, "model": "", "role": 0})
        lonely = doc.place_marker(templates["nav_node"], (-900.0, 0.0, 900.0))
        patrol = doc.place_marker(templates["bot"], (-900.0, 0.0, 880.0), values={"ai_slot": 8, "role": 2, "patrol_start": lonely,
                                                                                   "model": self.f.models["tank_placeholder"]})
        capture = doc.place_marker(templates["bot"], (-900.0, 0.0, 860.0), values={"ai_slot": 9, "role": 3, "model": self.f.models["tank_placeholder"],
                                                                                    "capture_target": self.f.ids("capture_zone")[0]})
        wrong = doc.place_marker(templates["bot"], (0.0, 0.0, 0.0), values={"ai_slot": 10, "role": 3, "model": self.f.models["tank_placeholder"],
                                                                            "capture_target": self.f.ids("trigger_volume")[0]})
        found = {(p.code, p.target) for p in self.check()}
        expected = {("MISSING_MODEL", missing), ("MISSING_MODEL", unknown), ("QUEENLORD_UNRESOLVED", queen), ("INVALID_FIELD", queen),
                    ("BAD_PATROL", patrol), ("UNREACHABLE_TARGET", capture), ("INVALID_REFERENCE", wrong)}
        self.assertTrue(expected <= found, expected - found)

    def test_no_navigation_means_moving_bots_cannot_be_checked(self):
        library = self.f.library
        project = projects.StudioProject("Empty", "studio")
        project.add_blank_map("m", "M", 1024)
        doc = project.open_map("m", library)
        doc.place_marker(self.f.templates["bot"], (0.0, 0.0, 0.0), values={"model": self.f.models["tank_placeholder"], "role": 0})
        self.assertIn("NO_NAVIGATION", codes(bots.check_bots(doc, library)))

    def test_placing_a_bot_claims_the_next_free_slot(self):
        self.assertEqual(bots.next_free_slot(self.f.doc.scene), 2)
        self.assertEqual(bots.required_features(self.f.doc.record(self.bots[1])), ["bot", "bot.kind.tank", "bot.role.patrol"])


class PlacementSlotTests(unittest.TestCase):
    def test_placing_bots_through_the_editor_claims_free_slots(self):
        from tests.test_studio_product import TempHome
        from tools.btga_studio.core.controller import EditorController
        from tools.btga_studio.core.settings import Settings

        with TempHome():
            c = EditorController(Settings.open(), log_to_disk=False, use_registry=False)
            self.addCleanup(c.shutdown)
            c.open_project(ROOT / "examples" / "starter-example.btgaproj")
            c.switch_map("training_grounds")
            c.viewport = (800, 600)
            template = next(t["id"] for t in c.library.starter()["templates"] if t["source_kind"] == "studio_bot")
            placed = []
            for _ in range(2):
                c.begin_placement("marker", template, {"model": c.library.starter()["models"]["tank_placeholder"]})
                placed.append(c.place_at(400, 300))
            slots = [c.document.record(p)["fields"]["ai_slot"]["value"] for p in placed]
            self.assertEqual(slots, [2, 3])  # the example already uses slots 0 and 1


class RuntimeContractTests(unittest.TestCase):
    def setUp(self):
        self.temp = Path(tempfile.mkdtemp(prefix="studio-runtime-"))
        self.f = Fixture()
        self.rom = self.temp / "cartridge.z64"
        self.rom.write_bytes(b"\x80\x37\x12\x40" + bytes(4092))
        self.package = self.temp / "out.btgapkg"
        data = package.build_package(self.f.project, self.f.library)
        self.package.write_bytes(data)
        # A project whose content came from this cartridge records its hash (as compute_dependencies does for game content).
        self.f.project.dependencies["sources"] = [{"platform": "n64_btga", "rom_sha256": runtime.rom_sha256(self.rom)}]
        self.export = runtime.ExportRecord(str(self.package), hashlib.sha256(data).hexdigest(), len(data), runtime.content_sha256(self.f.project))
        self.exe = self.temp / "runtime" / "btga-pc"
        self.exe.parent.mkdir()
        self.exe.write_bytes(b"#!/bin/sh\n")
        documents = {"training_grounds": self.f.doc}
        self.features = sorted(runtime.project_features(self.f.project, documents))
        self.write_sidecar(self.features)

    def write_sidecar(self, features, **extra):
        data = {"schema": runtime.CAPABILITIES_SCHEMA, "schema_version": 1, "runtime": {"name": "Test runtime", "version": "0.0.1"},
                "launch_request_versions": [1], "package_schema_versions": [1], "games": ["n64_btga"], "features": features,
                "bot_slots": 8, "teams": 4, "capture_modes": ["none", "screenshot"], **extra}
        (self.exe.parent / runtime.SIDECAR).write_text(json.dumps(data))

    def plan(self, **changes):
        arguments = {"export": self.export, "executable": self.exe, "rom_path": self.rom, "map_key": "training_grounds",
                     "scenario_key": "first_strike", "player_spawn": None, "bot_slots": [0, 1], "capture_mode": "none",
                     "documents": {"training_grounds": self.f.doc}, **changes}
        return runtime.preflight(self.f.project, arguments.pop("export"), arguments.pop("executable"), arguments.pop("rom_path"),
                                 arguments.pop("map_key"), arguments.pop("scenario_key"), arguments.pop("player_spawn"), arguments.pop("bot_slots"),
                                 arguments.pop("capture_mode"), self.temp / "captures", arguments.pop("documents"))

    def test_a_compatible_runtime_gets_a_valid_request(self):
        plan = self.plan()
        self.assertTrue(plan.ok, plan.problems)
        request = plan.request
        self.assertEqual(runtime.validate_request(request), [])
        self.assertEqual(request["package"]["sha256"], self.export.sha256)
        self.assertEqual(request["rom"]["path"], str(self.rom.resolve()))
        self.assertEqual(request["bot_slots"], [0, 1])
        self.assertEqual(len(request["session_id"]), 32)
        self.assertNotEqual(self.plan().request["session_id"], request["session_id"])  # unique per launch
        self.assertNotIn(str(self.rom), projects.dumps(self.f.project))  # the ROM path never enters the project

    def test_each_failure_is_named(self):
        cases = {
            "RUNTIME_ABSENT": {"executable": self.temp / "nowhere"},
            "PACKAGE_MISSING": {"export": None},
            "ROM_MISSING": {"rom_path": self.temp / "gone.z64"},
            "INVALID_SELECTION": {"map_key": "no_such_map"},
        }
        for code, change in cases.items():
            self.assertIn(code, {c for c, _ in self.plan(**change).problems}, code)
        other = self.temp / "other.z64"
        other.write_bytes(b"\x80\x37\x12\x40" + bytes(10))
        self.assertIn("ROM_MISMATCH", {c for c, _ in self.plan(rom_path=other).problems})
        self.write_sidecar(self.features[:-3])
        self.assertIn("CONTENT_REJECTED", {c for c, _ in self.plan().problems})
        self.write_sidecar(self.features, launch_request_versions=[2])
        self.assertIn("RUNTIME_INCOMPATIBLE", {c for c, _ in self.plan().problems})
        (self.exe.parent / runtime.SIDECAR).unlink()
        self.assertIn("RUNTIME_INCOMPATIBLE", {c for c, _ in self.plan().problems})

    def test_map_record_selections_must_exist_and_be_enabled(self):
        self.assertIn("INVALID_SELECTION", {c for c, _ in self.plan(player_spawn="missing").problems})
        self.assertIn("INVALID_SELECTION", {c for c, _ in self.plan(bot_slots=[0, 7]).problems})
        self.assertIn("INVALID_SELECTION", {c for c, _ in self.plan(documents={}).problems})

    def test_stale_packages_are_refused(self):
        self.f.project.scenarios["first_strike"]["title"] = "Changed after export"
        self.assertIn("PACKAGE_STALE", {c for c, _ in self.plan().problems})
        self.f.project.scenarios["first_strike"]["title"] = "First strike"
        self.assertTrue(self.plan().ok)
        self.package.write_bytes(self.package.read_bytes() + b"x")
        self.assertIn("PACKAGE_STALE", {c for c, _ in self.plan().problems})

    def test_launch_writes_the_request_to_stdin_and_never_falls_back(self):
        class Process:
            def __init__(self, args, stdin, cwd):
                self.args, self.stdin = args, io.BytesIO()
                self.stdin.close = lambda: None
                started.append(self)

        started = []
        plan = self.plan()
        runtime.launch(plan, self.exe, popen=Process)
        self.assertEqual(started[0].args, [str(self.exe), runtime.LAUNCH_ARGUMENT, "-"])
        self.assertEqual(json.loads(started[0].stdin.getvalue())["session_id"], plan.request["session_id"])
        broken = self.plan(export=None)
        with self.assertRaises(runtime.RuntimeProblem):
            runtime.launch(broken, self.exe, popen=Process)
        self.assertEqual(len(started), 1)

    def test_request_schema_rejects_bad_requests(self):
        request = self.plan().request
        for key, value in (("schema_version", 2), ("session_id", "x"), ("bot_slots", [1, 1]), ("capture", {"mode": "video", "directory": None}),
                           ("rom", {"path": "relative.z64", "sha256": "0" * 64})):
            self.assertTrue(runtime.validate_request({**request, key: value}), key)

    def test_playability_is_never_assumed(self):
        required = runtime.project_features(self.f.project, {"training_grounds": self.f.doc})
        self.assertEqual(runtime.playability(required, None)["status"], "unverified")
        capabilities = json.loads((self.exe.parent / runtime.SIDECAR).read_text())
        self.assertEqual(runtime.playability(required, capabilities)["status"], "supported")
        capabilities["features"] = capabilities["features"][1:]
        report = runtime.playability(required, capabilities)
        self.assertEqual((report["status"], len(report["missing"])), ("unsupported", 1))


class PackageRepresentationTests(unittest.TestCase):
    def test_package_carries_scenarios_items_features_and_dependencies(self):
        f = Fixture()
        data = package.build_package(f.project, f.library)
        self.assertEqual(data, package.build_package(example_project(), f.library))  # deterministic
        read = package.read_package(data)
        self.assertEqual(read.project["scenarios"], json.loads(projects.dumps(f.project))["scenarios"])
        self.assertEqual(set(read.project["items"]), {"repair_kit", "speed_boost"})
        self.assertEqual(read.manifest["playability"]["status"], "unverified")
        self.assertIn("objective.capture", read.manifest["features_required"])
        self.assertEqual(read.dependencies["schema"], authoring.REPORT_SCHEMA)
        self.assertEqual(read.dependencies["unresolved"], [])

    def test_unresolved_game_resources_block_export_instead_of_being_replaced(self):
        f = Fixture()
        game_model = "n64_btga:model:" + "a" * 24
        bot = f.ids("bot_slot")[0]
        f.doc.set_field(bot, "model", game_model)
        report = package.validate_for_export(f.project, f.library)
        self.assertTrue(any(e["code"] == "MISSING_MODEL" and e["target"] == bot for e in report.errors))
        with self.assertRaises(package.PackageError):
            package.build_package(f.project, f.library)
        dependency = authoring.dependency_report(f.project, f.library, {"training_grounds": f.doc})
        self.assertEqual(dependency["unresolved"], [game_model])
        row = dependency["game_resources"][0]
        self.assertEqual((row["kind"], row["status"]), ("model", "unresolved"))
        self.assertIn(f"maps:training_grounds:{bot}", row["used_by"])

    def test_reader_rejects_malformed_additions(self):
        f = Fixture()
        data = package.build_package(f.project, f.library)
        import zipfile

        def rewrite(name, content):
            source = zipfile.ZipFile(io.BytesIO(data))
            files = {i.filename: source.read(i.filename) for i in source.infolist()}
            files[name] = content
            manifest = json.loads(files["manifest.json"])
            for entry in manifest["files"]:
                if entry["path"] == name:
                    entry.update({"sha256": hashlib.sha256(content).hexdigest(), "size": len(content)})
            files["manifest.json"] = json.dumps(manifest).encode()
            return package._zip(files)

        with self.assertRaises(package.PackageError):
            package.read_package(rewrite("dependencies.json", b"[]"))
        project = json.loads(zipfile.ZipFile(io.BytesIO(data)).read("project.json"))
        project["scenarios"] = []
        with self.assertRaises(package.PackageError):
            package.read_package(rewrite("project.json", json.dumps(project).encode()))


class OverlayGeometryTests(unittest.TestCase):
    def test_authoring_overlays_draw_areas_arrows_and_bot_links(self):
        try:
            from tools.btga_studio.gui.renderer import authoring_segments, navigation_segments
        except Exception as error:  # OpenGL is not importable here
            self.skipTest(f"renderer unavailable: {error}")
        f = Fixture()
        segments = authoring_segments(f.doc.scene)
        colours = {tuple(colour) for _, colour in segments}
        from tools.btga_studio.gui.renderer import LAYER_COLOURS

        for name in ("trigger_volume", "capture_zone", "spawn", "bot"):
            self.assertIn(LAYER_COLOURS[name], colours, name)
        self.assertEqual(len(segments) % 2, 0)  # line pairs
        hidden = authoring_segments(f.doc.scene, set(f.ids("trigger_volume")))
        self.assertLess(len(hidden), len(segments))
        nodes = f.ids("navigation_node")
        reach = navigation_segments(f.doc.scene, frozenset(), {nodes[0]})
        node_colours = {tuple(colour) for _, colour in reach}
        self.assertTrue({LAYER_COLOURS["reachable"], LAYER_COLOURS["unreachable"]} <= node_colours)


class ContractDocumentTests(unittest.TestCase):
    def test_feature_matrix_document_matches_the_code(self):
        from tools.runtime_feature_matrix import matrix

        self.assertEqual((ROOT / "docs" / "RUNTIME_FEATURES.md").read_text(encoding="utf-8"), matrix(None))
        self.assertIn("pending", matrix(None))
        self.assertNotIn("| declared |", matrix(None))

    def test_json_schemas_agree_with_the_validators(self):
        request_schema = json.loads((ROOT / "docs" / "schemas" / "btga-launch-request-v1.schema.json").read_text(encoding="utf-8"))
        capabilities_schema = json.loads((ROOT / "docs" / "schemas" / "btga-runtime-capabilities-v1.schema.json").read_text(encoding="utf-8"))
        self.assertEqual(request_schema["properties"]["schema"]["const"], runtime.LAUNCH_SCHEMA)
        self.assertEqual(request_schema["properties"]["schema_version"]["const"], runtime.LAUNCH_VERSION)
        self.assertEqual(request_schema["properties"]["capture"]["properties"]["mode"]["enum"], list(runtime.CAPTURE_MODES))
        self.assertEqual(capabilities_schema["properties"]["schema"]["const"], runtime.CAPABILITIES_SCHEMA)
        sample = {"schema": runtime.LAUNCH_SCHEMA, "schema_version": 1, "session_id": "0" * 32,
                  "package": {"path": str(ROOT / "x.btgapkg"), "sha256": "0" * 64, "size": 1}, "map": "m", "scenario": None,
                  "player_spawn": None, "bot_slots": [], "capture": {"mode": "none", "directory": None},
                  "rom": {"path": str(ROOT / "x.z64"), "sha256": "0" * 64}, "studio": {"version": "0"}, "features": []}
        self.assertEqual(runtime.validate_request(sample), [])
        for key in request_schema["required"]:
            broken = dict(sample)
            broken.pop(key)
            self.assertTrue(runtime.validate_request(broken), key)


class ActionVocabularyTests(unittest.TestCase):
    def test_every_action_has_defaults_that_validate_without_references(self):
        ctx = act.ActionContext("scenario", check_refs=False)
        for kind, spec in act.ACTIONS.items():
            action = act.new_action(kind)
            problems = act.check_action(action, ctx)
            self.assertTrue(all("choose the" in message for _, message in problems), (kind, problems))
            self.assertIn(act.feature(kind), runtime.FEATURES)

    def test_number_ranges_are_enforced(self):
        ctx = act.ActionContext("item", check_refs=False)
        too_much = {"do": "modify_stat", "stat": "health", "amount": 10**9, "mode": "add"}
        self.assertEqual([name for name, _ in act.check_action(too_much, ctx)], ["amount"])
        self.assertEqual([name for name, _ in act.check_action({"do": "timed_modifier", "stat": "speed", "factor": 50.0, "seconds": 0}, ctx)],
                         ["factor", "seconds"])

    def test_contexts_are_enforced(self):
        item_ctx = act.ActionContext("item", check_refs=False)
        self.assertTrue(act.check_action({"do": "end_scenario", "result": "victory"}, item_ctx))
        self.assertFalse(act.check_action({"do": "message", "text": "Hi", "seconds": 3, "audience": "everyone"}, item_ctx))
        self.assertTrue(act.check_action({"do": "message", "text": "bad\x07", "seconds": 3, "audience": "everyone"}, item_ctx))


@unittest.skipIf(not _egl_available(), "needs imgui-bundle, PyOpenGL and EGL (off-screen UI)")
class AuthoringWindowTests(unittest.TestCase):
    def test_windows_draw_and_edits_undo_as_single_steps(self):
        os.environ.setdefault("PYOPENGL_PLATFORM", "egl")
        from tests.test_studio_product import TempHome
        from tools.btga_studio.core.settings import Settings
        from tools.btga_studio.fixtures import recording_launcher
        from tools.btga_studio.gui.app import StudioApp
        from tools.btga_studio.gui.offscreen import OffscreenUI

        with TempHome() as home, recording_launcher():
            app = StudioApp(Settings.open())
            app.settings.preferences.welcome_seen = True
            ui = OffscreenUI(app, 1600, 960)
            self.addCleanup(ui.close)
            c = app.controller
            self.addCleanup(c.shutdown)
            app.show_hub = False
            self.assertTrue(app.open_project(ROOT / "examples" / "starter-example.btgaproj"))
            app.switch_map("training_grounds")
            app.open_scenarios("first_strike")
            app.scenario_element = ("objectives", "obj2")
            for _ in range(3):
                ui.frame()
            app.show_items, app.item_key = True, "speed_boost"
            bot = [r["id"] for _, r in c.document.scene.records("markers") if r.get("category") == "bot_slot"][1]
            c.frame_record(bot)
            for _ in range(3):
                ui.frame()
            before = copy.deepcopy(c.project.scenarios)
            app.scenario_change("Add objective", lambda: scn.add_element(c.project.scenarios["first_strike"], "objectives", "survive"))
            app.undo()
            self.assertEqual(c.project.scenarios, before)
            self.assertFalse(app.runtime_state()[0])  # no runtime configured: Test in Game stays disabled
            self.assertEqual(app.marker_models(c.document.record(bot)), [c.library.starter()["models"]["tank_placeholder"]])
            log = Path(home) / "logs" / "studio.log"
            errors = [line for line in log.read_text().splitlines() if '"level": "error"' in line] if log.exists() else []
            self.assertEqual(errors, [])


if __name__ == "__main__":
    unittest.main()
