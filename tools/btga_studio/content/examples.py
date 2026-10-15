"""ROM-free example project built only from Studio starter content.

It contains a small mission map with a one-mission campaign and its
scenario (ordered and optional objectives, a trigger volume, a capture
zone, a destructible target, a timer, a counter, a wave and two bots), item
definitions with pickups, a multiplayer arena, a target-range minigame,
placeholder tanks and two gangs.
Generation is deterministic (fixed project ID, fixed layout), so the
committed example equals a fresh build.

    python -m tools.btga_studio.content.examples examples/starter-example.btgaproj
"""

from __future__ import annotations

import argparse
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.btga_studio.content.starter import ensure_starter_catalog  # noqa: E402
from tools.btga_studio.core.library import LibrarySet  # noqa: E402
from tools.btga_studio.core import items as items_module  # noqa: E402
from tools.btga_studio.core import scenario as scenario_module  # noqa: E402
from tools.btga_studio.core.project import StudioProject, save_project  # noqa: E402

EXAMPLE_ID = "5747a2e1c0ffee01"


def _tiles(document, models, half: int) -> None:
    for x in range(-half, half, 512):
        for z in range(-half, half, 512):
            document.place_model(models["ground_tile"], (x + 256.0, 0.0, z + 256.0))


def _marker(document, templates, kind, point, yaw=0, values=None) -> str:
    return document.place_marker(templates[kind], point, yaw, values=values or {})


def build_example(library: LibrarySet) -> StudioProject:
    starter = library.starter()
    models = starter["models"]
    templates = {t["source_kind"].removeprefix("studio_"): t for t in starter["templates"]}
    project = StudioProject("Starter example", "studio", EXAMPLE_ID)
    project.project["description"] = "Original placeholder content only; no game data."

    project.add_blank_map("training_grounds", "Training grounds", 1024)
    doc = project.open_map("training_grounds", library)
    _tiles(doc, models, 1024)
    doc.place_model(models["road_straight"], (0.0, 0.0, 0.0))
    for x, z in ((-500.0, -400.0), (500.0, -400.0)):
        doc.place_model(models["building_small"], (x, 0.0, z))
    doc.place_model(models["building_tall"], (0.0, 0.0, -700.0))
    doc.place_model(models["wall"], (-300.0, 0.0, 300.0), 0x4000)
    doc.place_model(models["ramp"], (300.0, 0.0, 300.0))
    _marker(doc, templates, "player_spawn", (0.0, 0.0, 800.0), 0x8000)
    target = doc.place_model(models["cube"], (0.0, 0.0, -400.0))
    objective = _marker(doc, templates, "objective", (0.0, 0.0, -300.0), values={"target": target, "label": "Destroy the depot"})
    _marker(doc, templates, "unit_generator", (-600.0, 0.0, -800.0), values={"team": 1, "spawn_budget": 6})
    _marker(doc, templates, "pickup", (400.0, 0.0, 500.0), values={"pickup_type": 1})
    _marker(doc, templates, "hazard", (-400.0, 0.0, 100.0))
    nodes = [_marker(doc, templates, "nav_node", point) for point in ((0.0, 0.0, 600.0), (0.0, 0.0, 0.0), (0.0, 0.0, -600.0))]
    doc.set_field(nodes[0], "link_0", nodes[1])
    doc.set_field(nodes[1], "link_0", nodes[2])
    doc.set_field(nodes[1], "link_1", nodes[0])
    doc.set_field(nodes[2], "link_0", nodes[1])  # the far end leads back, so a patrol can turn round
    spawn = next(r["id"] for _, r in doc.scene.records("markers") if r.get("category") == "player_spawn")
    gate = _marker(doc, templates, "trigger_volume", (0.0, 0.0, 200.0), values={"radius": 160, "label": "Gate"})
    hill = _marker(doc, templates, "capture_zone", (500.0, 0.0, 0.0), values={"radius": 220, "label": "East hill"})
    depot = _marker(doc, templates, "destructible_target", (0.0, 0.0, -480.0), values={"health": 800, "label": "Fuel depot"})
    escort = _marker(doc, templates, "bot", (-150.0, 0.0, 700.0), 0x8000, values={"ai_slot": 0, "team": 0, "role": 5, "model": models["tank_placeholder"]})
    _marker(doc, templates, "bot", (0.0, 0.0, -600.0), 0, values={"ai_slot": 1, "team": 1, "role": 2, "patrol_start": nodes[2],
                                                                 "model": models["tank_placeholder"], "aggression": 70})
    project.define("items", "repair_kit", items_module.new_item("repair_kit"))
    project.define("items", "speed_boost", items_module.new_item("speed_boost"))
    for x in (-400.0, 400.0):
        _marker(doc, templates, "item_pickup", (x, 0.0, 600.0), values={"item": "repair_kit", "respawn_seconds": 45})
    _marker(doc, templates, "item_pickup", (0.0, 0.0, 450.0), values={"item": "speed_boost"})
    scenario = scenario_module.new_scenario("First strike", "training_grounds")
    scenario["start"].update({"player_spawn": spawn, "briefing": "Drive through the gate, take the east hill, then destroy the fuel depot."})
    reach = scenario_module.add_element(scenario, "objectives", "reach", "Drive through the gate")
    scenario["objectives"][reach]["params"]["area"] = gate
    take = scenario_module.add_element(scenario, "objectives", "capture", "Take the east hill")
    scenario["objectives"][take]["params"].update({"zone": hill, "hold_seconds": 15})
    scenario["objectives"][take]["requires"] = [reach]
    smash = scenario_module.add_element(scenario, "objectives", "destroy", "Destroy the fuel depot")
    scenario["objectives"][smash]["params"]["targets"] = [depot]
    scenario["objectives"][smash]["requires"] = [take]
    bonus = scenario_module.add_element(scenario, "objectives", "marker_objective", "Bonus: the depot marker")
    scenario["objectives"][bonus].update({"optional": True, "params": {"marker": objective}})
    clock = scenario_module.add_element(scenario, "timers", "Reinforcements")
    scenario["timers"][clock].update({"seconds": 90, "starts": "by_action"})
    kills = scenario_module.add_element(scenario, "counters", "Tanks destroyed")
    wave = scenario_module.add_element(scenario, "waves", "Counter-attack")
    scenario["waves"][wave].update({"spawner": next(r["id"] for _, r in doc.scene.records("markers") if r.get("category") == "unit_spawner"),
                                    "count": 3})
    trigger = scenario_module.add_element(scenario, "triggers", "At the gate")
    scenario["triggers"][trigger].update({"volume": gate, "actions": [
        {"do": "message", "text": "The east hill is guarded. Take it!", "seconds": 5, "audience": "player"},
        {"do": "start_timer", "timer": clock}]})
    scenario["timers"][clock]["on_expire"] = [{"do": "spawn_wave", "wave": wave}, {"do": "counter_add", "counter": kills, "amount": 0}]
    scenario["objectives"][smash]["on_complete"] = [{"do": "message", "text": "Depot destroyed!", "seconds": 4, "audience": "everyone"}]
    group = scenario_module.add_element(scenario, "groups", "Escort")
    scenario["groups"][group]["members"] = [escort]
    project.define("scenarios", "first_strike", scenario)
    project.define("missions", "first_strike", {"title": "First strike", "map": "training_grounds", "objectives": [objective], "scenario": "first_strike",
                                                "briefing": "Destroy the depot at the north end of the training grounds."})
    project.campaigns.append({"id": "tutorial", "title": "Tutorial", "missions": ["first_strike"]})

    project.add_blank_map("arena", "Square arena", 1024)
    doc = project.open_map("arena", library)
    _tiles(doc, models, 1024)
    for index, (x, z, yaw) in enumerate(((-800.0, -800.0, 0x2000), (800.0, -800.0, 0xE000), (-800.0, 800.0, 0x6000), (800.0, 800.0, 0xA000))):
        _marker(doc, templates, "team_start", (x, 0.0, z), yaw, {"team": index})
    for x in (-300.0, 300.0):
        doc.place_model(models["wall"], (x, 0.0, 0.0), 0x4000)
    _marker(doc, templates, "pickup", (0.0, 0.0, 0.0), values={"pickup_type": 2})
    project.define("multiplayer", "square_arena", {"title": "Square arena", "map": "arena", "modes": ["deathmatch"], "max_players": 4})

    project.add_blank_map("target_range", "Target range", 1024)
    doc = project.open_map("target_range", library)
    _tiles(doc, models, 1024)
    _marker(doc, templates, "player_spawn", (0.0, 0.0, 800.0), 0x8000)
    for index in range(5):
        doc.place_model(models["cube"], (-600.0 + 300.0 * index, 0.0, -500.0))
    project.define("minigames", "target_practice", {"title": "Target practice", "map": "target_range", "rules": {"time_limit_seconds": 120, "targets": 5}})

    project.define("tanks", "scout", {"title": "Scout", "model": models["tank_placeholder"], "stats": {"speed": 1.2, "armor": 60, "weapon": "cannon"}})
    project.define("tanks", "heavy", {"title": "Heavy", "model": models["tank_placeholder"], "stats": {"speed": 0.7, "armor": 140, "weapon": "cannon"}})
    project.define("gangs", "blue_team", {"title": "Blue team", "colour": [50, 90, 210], "tanks": ["scout", "heavy"]})
    project.define("gangs", "red_team", {"title": "Red team", "colour": [200, 50, 50], "tanks": ["scout"]})
    project.compute_dependencies(library)
    return project


def write_example(path: Path, starter_directory: Path | None = None) -> Path:
    with tempfile.TemporaryDirectory() as temp:
        root = ensure_starter_catalog(Path(starter_directory) if starter_directory else Path(temp))
        project = build_example(LibrarySet.open_all([root]))
        save_project(Path(path), project)
    return Path(path)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("output", type=Path)
    args = parser.parse_args(argv)
    write_example(args.output)
    print(f"example project written to {args.output.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
