"""Add recognisable Global Assault objects to a built Minneapolis project ("Minneapolis + Global Assault").

Reads a folder written by ``build_minneapolis_map.py`` (its project and Studio
home with the imported city library) and a catalog the Source Manager built
from the user's own cartridge, and writes a new project and package next to
it.  The city geometry is not touched: everything here is added as new
records on top of it.

Added, all as references to game content IDs (no game data is copied into
the project or the package; the package's dependencies.json lists every ID
and the cartridge hash it comes from):

* a team base at each team start: a destructible target drawn with the
  game's multiplayer base (intact, and damage state 5 as its wreck);
* a Queenlord bot defending each base (model identification: inferred, see
  ``core.game_models``);
* tank bots drawn with the game's tank (hull and turret): a patrol, a zone
  capturer, an escort and a boss;
* item definitions drawn with the game's crates, placed as item pickups;
* destructible props (shed, watchtower, fence, spire) as destructible targets;
* tank traps and concrete barriers ringing an open square near The Commons
  (the builder looks for open, level ground so the ring is visible);
* a capture zone on that square, a trigger volume at the IDS ramp, and a
  scenario that uses them;
* a tank definition using the game's tank.

    python tools/build_minneapolis_btga.py BUILD_FOLDER CATALOG_FOLDER OUTPUT_FOLDER

Every step validates; the script stops with the validation messages if the
result has errors.  Paths given on the command line never enter the project.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import shutil
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

TITLE = "Minneapolis + Global Assault"
IDS_RAMP = (-9655.0, 9373.0)  # the ramp in front of the IDS Center (Studio units)
RING = 560.0  # barrier ring radius around the square (35 m)


def build(build_folder: Path, catalog: Path, output: Path) -> dict:
    os.environ["BTGA_STUDIO_HOME"] = str(output / "studio-home")
    if (output / "studio-home").exists():
        shutil.rmtree(output / "studio-home")
    output.mkdir(parents=True, exist_ok=True)
    shutil.copytree(build_folder / "studio-home" / "user-library", output / "studio-home" / "user-library")

    from tools.btga_studio.core import authoring, bots, game_models, items, package, scenario as scn
    from tools.btga_studio.core import project as projects
    from tools.btga_studio.core.controller import EditorController
    from tools.btga_studio.core.drive import HeightIndex
    from tools.btga_studio.core.navgraph import NavGraph
    from tools.btga_studio.core.settings import Settings

    started = time.time()
    steps = []

    def note(text):
        steps.append({"t": round(time.time() - started, 1), "step": text})
        print(f"[{time.time() - started:6.1f}s] {text}", flush=True)

    c = EditorController(Settings.open(), log_to_disk=False, use_registry=False)
    c.open_catalog(catalog)
    c.open_project(build_folder / "Minneapolis.btgaproj")
    project = c.project
    key = next(iter(project.maps))
    c.switch_map(key)
    doc = c.document
    library = c.library
    project.project["title"] = TITLE
    project.project["description"] = (project.project.get("description", "") + "\n\nGlobal Assault objects (team bases, Queenlords, tanks, "
                                      "crates, destructible props and barriers) are referenced by content ID from the user's own cartridge.")[:200]
    templates = {t["source_kind"].removeprefix("studio_"): t for t in library.starter()["templates"]}
    missing = [e["label"] for e in game_models.ENTRIES if e["id"] not in library._models]
    if missing:
        raise SystemExit(f"the catalog lacks game models: {missing}")
    note("indexing the ground for placement heights")
    heights = HeightIndex(doc.scene)

    def ground(x, z):
        value = heights.height(x, z, 1e9)
        return 0.0 if value is None else value

    def at(x, z):
        return (float(x), ground(x, z), float(z))

    markers = {r["id"]: r for _, r in doc.scene.records("markers")}
    starts = sorted((r for r in markers.values() if r.get("category") == "team_start"), key=lambda r: r["fields"]["team"]["value"])
    spawns = {r["fields"]["team"]["value"]: r for r in markers.values() if r.get("category") == "player_spawn"}
    counts = {}

    def count(name):
        counts[name] = counts.get(name, 0) + 1

    # -- team bases and Queenlords -----------------------------------------------------------
    note("team bases and Queenlords")
    bases, queens = {}, {}
    for start in starts:
        team = start["fields"]["team"]["value"]
        theta = start["yaw"] * 2 * math.pi / 65536
        back = (math.sin(theta), math.cos(theta))  # behind the start (forward is -sin, -cos)
        bx, bz = start["translation"][0] + back[0] * 420, start["translation"][2] + back[1] * 420
        bases[team] = doc.place_marker(templates["destructible_target"], at(bx, bz), start["yaw"], values={
            "model": game_models.BASE_STATES[0], "destroyed_model": game_models.BASE_STATES[5], "health": 4000, "owner": team, "score": 2000,
            "label": f"Team {team} base"})
        count("team bases")
        queens[team] = doc.place_marker(templates["bot"], at(bx + 160, bz + 160), start["yaw"], values={
            "ai_slot": team, "kind": 1, "model": game_models.QUEENLORD, "team": team, "role": 1, "guard_target": bases[team], "aggression": 10,
            "allowed_weapons": 1})
        count("Queenlords")

    # -- an open square near The Commons: capture zone and barrier ring; a trigger at the IDS ramp ----------------
    note("square zone, trigger and barriers")
    commons = starts[1]["translation"]

    def open_ground(cx, cz):
        level = ground(cx, cz)
        points = [(cx + math.cos(a) * r, cz + math.sin(a) * r) for r in (RING * 0.5, RING, RING + 120) for a in
                  (2 * math.pi * k / 16 for k in range(16))]
        return all(abs(ground(x, z) - level) < 30.0 for x, z in points)

    candidates = sorted(((commons[0] + dx, commons[2] + dz) for dx in range(-2400, 2401, 300) for dz in range(-2400, 2401, 300)),
                        key=lambda p: (math.dist(p, (commons[0], commons[2])), p))
    square = next((p for p in candidates if math.dist(p, (commons[0], commons[2])) > 700 and open_ground(*p)), None)
    if square is None:
        raise SystemExit("no open, level square found near The Commons")
    plaza = doc.place_marker(templates["capture_zone"], at(*square), values={"radius": 420, "label": "Commons square"})
    ramp = doc.place_marker(templates["trigger_volume"], at(*IDS_RAMP), values={"radius": 240, "label": "IDS ramp"})
    barrier_models = [e["id"] for e in game_models.ENTRIES if e["role"] == "barrier" and e["label"] != "Guard rail"]
    for number in range(10):
        angle = 2 * math.pi * number / 10
        x, z = square[0] + math.cos(angle) * RING, square[1] + math.sin(angle) * RING
        doc.place_model(barrier_models[number % len(barrier_models)], at(x, z), int((angle + math.pi / 2) * 65536 / (2 * math.pi)) & 0xFFFF)
        count("barriers")

    # -- destructible props -------------------------------------------------------------------
    note("destructible props")
    props = [e for e in game_models.ENTRIES if e["role"] == "destructible"]
    targets = []
    street = NavGraph.from_scene(doc.scene)  # navigation points sit on streets: put the props on the pavement beside them

    def pavement(node_number: int):
        nx, nz = street.nodes[sorted(street.nodes)[node_number % len(street.nodes)]]
        level = ground(nx, nz)
        for dx, dz in ((260, 0), (-260, 0), (0, 260), (0, -260), (190, 190), (-190, -190)):
            if abs(ground(nx + dx, nz + dz) - level) < 24.0:  # still street level, not a roof or a wall top
                return nx + dx, nz + dz
        return nx, nz

    for number, (x, z) in enumerate(pavement(n) for n in (1, 5, 9, 13, 17, 21)):
        entry = props[number % len(props)]
        targets.append(doc.place_marker(templates["destructible_target"], at(x, z), (number * 0x2800) & 0xFFFF, values={
            "model": entry["id"], "destroyed_model": entry["wreck"], "health": 600, "label": entry["label"].replace(" (intact)", "")}))
        count("destructible props")

    # -- items and pickups ---------------------------------------------------------------------
    note("items drawn with the game's crates")
    crates = {e["label"]: e["id"] for e in game_models.ENTRIES if e["role"] == "crate"}
    for item_key, preset, crate in (("repair_kit", "repair_kit", "Crate: red cross"), ("ammo_crate", "ammo_crate", "Crate: shells"),
                                    ("rocket_pod", "rocket_pod", "Crate: target"), ("speed_boost", "speed_boost", "Crate: lightning"),
                                    ("score_star", "score_star", "Crate: star")):
        project.define("items", item_key, items.new_item(preset, crates[crate]))
    graph = NavGraph.from_scene(doc.scene)
    nodes = sorted(graph.nodes)
    for number, item_key in enumerate(("repair_kit", "ammo_crate", "rocket_pod", "speed_boost", "score_star", "score_star", "score_star",
                                       "repair_kit")):
        x, z = graph.nodes[nodes[(number * 4 + 2) % len(nodes)]]
        doc.place_marker(templates["item_pickup"], at(x + 90, z + 90), values={"item": item_key, "respawn_seconds": 40})
        count("item pickups")

    # -- tank bots --------------------------------------------------------------------------------
    note("tank bots")
    patrol_start = next(n for n in nodes if len(set(graph.patrol_route(n))) >= 3)
    px, pz = graph.nodes[patrol_start]
    tank = game_models.TANK_HULL
    patrol = doc.place_marker(templates["bot"], at(px + 120, pz), values={"ai_slot": 4, "team": 1, "role": 2, "patrol_start": patrol_start,
                                                                           "model": tank, "aggression": 60})
    capture = doc.place_marker(templates["bot"], at(square[0] + 300, square[1] - 150), values={
        "ai_slot": 5, "team": 2, "role": 3, "capture_target": plaza, "capture_behavior": 1, "model": tank, "aggression": 75})
    escort = doc.place_marker(templates["bot"], at(starts[0]["translation"][0] + 300, starts[0]["translation"][2]), starts[0]["yaw"], values={
        "ai_slot": 6, "team": 0, "role": 4, "escort_target": queens[0], "model": tank, "aggression": 40})
    boss_x, boss_z = graph.nodes[nodes[len(nodes) // 2]]
    boss = doc.place_marker(templates["bot"], at(boss_x + 150, boss_z + 150), values={"ai_slot": 7, "team": 3, "role": 6, "model": tank,
                                                                                       "aggression": 100, "respawn": 0})
    counts["tank bots"] = 4

    # -- the scenario -------------------------------------------------------------------------------
    note("scenario")
    scenario = scn.new_scenario("Defend the Queenlord", key, "custom")
    scenario["rules"].update({"teams": 4, "time_limit_seconds": 900})
    scenario["start"].update({"player_spawn": spawns[0]["id"], "briefing": "Hold the Commons square, knock down the watchtower and "
                              "defeat the boss tank before it reaches your Queenlord."})
    hold = scn.add_element(scenario, "objectives", "capture", "Hold the Commons square")
    scenario["objectives"][hold]["params"].update({"zone": plaza, "hold_seconds": 60, "team": 0})
    tower = next(t for t in targets if doc.record(t)["fields"]["label"]["value"] == "Watchtower")
    smash = scn.add_element(scenario, "objectives", "destroy", "Knock down the watchtower")
    scenario["objectives"][smash]["params"]["targets"] = [tower]
    scenario["objectives"][smash]["requires"] = [hold]
    duel = scn.add_element(scenario, "objectives", "boss", "Defeat the boss tank")
    scenario["objectives"][duel]["params"]["boss"] = boss
    scenario["objectives"][duel]["requires"] = [smash]
    stars = scn.add_element(scenario, "objectives", "collect", "Bonus: collect three score stars")
    scenario["objectives"][stars].update({"optional": True, "params": {"item": "score_star", "count": 3}})
    guard = scn.add_element(scenario, "objectives", "defend", "Keep your Queenlord's base standing")
    scenario["objectives"][guard].update({"optional": True, "params": {"zone": plaza, "seconds": 600}})
    alarm = scn.add_element(scenario, "triggers", "Ramp alarm")
    scenario["triggers"][alarm].update({"volume": ramp, "actions": [{"do": "message", "text": "The ramp lifts you to the IDS roof.", "seconds": 4,
                                                                      "audience": "player"}]})
    clock = scn.add_element(scenario, "timers", "Boss arrives")
    scenario["timers"][clock].update({"seconds": 240, "starts": "at_start", "on_expire": [
        {"do": "message", "text": "The boss tank is coming!", "seconds": 5, "audience": "everyone"}]})
    scenario["failure"] = [{"when": "player_destroyed"}, {"when": "record_destroyed", "record": bases[0]}]
    scenario["groups"] = {}
    group = scn.add_element(scenario, "groups", "Bosses")
    scenario["groups"][group]["members"] = [boss]
    project.define("scenarios", "defend_the_queenlord", scenario)
    project.define("tanks", "global_assault_tank", {"title": "Global Assault tank", "model": tank, "stats": {"speed": 1.0, "armor": 100,
                                                                                                           "weapon": "cannon"}})

    # -- validate and write -------------------------------------------------------------------------
    note("validating")
    report = package.validate_for_export(project, library)
    if report.errors:
        for error in report.errors[:30]:
            print("ERROR", error, flush=True)
        raise SystemExit(f"{len(report.errors)} validation error(s)")
    project_path = output / "Minneapolis-Global-Assault.btgaproj"
    projects.save_project(project_path, project)
    note("exporting the package")
    data = package.build_package(project, library, "lab")
    (output / "Minneapolis-Global-Assault.btgapkg").write_bytes(data)
    documents = {key: doc}
    dependency = authoring.dependency_report(project, library, documents)
    (output / "dependency-report.json").write_text(json.dumps(dependency, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    read = package.read_package(data)
    result = {"title": TITLE, "counts": counts, "validation": {"errors": len(report.errors), "warnings": [w["code"] for w in report.warnings]},
              "package_bytes": len(data), "game_resources": len(dependency["game_resources"]), "unresolved": dependency["unresolved"],
              "sources": dependency["sources"], "playability": read.manifest["playability"]["status"],
              "features_required": len(read.manifest["features_required"]), "steps": steps, "seconds": round(time.time() - started, 1),
              "records": {"bases": bases, "queenlords": queens, "plaza": plaza, "ramp": ramp, "targets": targets, "bots": {
                  "patrol": patrol, "capture": capture, "escort": escort, "boss": boss}}}
    (output / "build-report.json").write_text(json.dumps(result, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    c.shutdown()
    return result


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("build", type=Path, help="folder written by build_minneapolis_map.py")
    parser.add_argument("catalog", type=Path, help="catalog folder built by the Source Manager from your cartridge")
    parser.add_argument("output", type=Path)
    args = parser.parse_args(argv)
    result = build(args.build.resolve(), args.catalog.resolve(), args.output.resolve())
    print(json.dumps({k: v for k, v in result.items() if k not in ("steps", "records")}, indent=1, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
