"""Off-screen evidence for Studio authoring: original Global Assault maps, Minneapolis with game objects, and the editors.

    python tools/capture_studio_authoring.py real-maps OUTPUT --home STUDIO_HOME
    python tools/capture_studio_authoring.py minneapolis OUTPUT --build BTGA_BUILD_FOLDER --catalog CATALOG [--rom ROM] [--stub-runtime DIR]
    python tools/capture_studio_authoring.py editor OUTPUT

``real-maps`` uses a Studio home whose Source Manager registered the user's
cartridge and built its catalog (the normal Studio path), opens three
original maps as imported maps and captures the application, the asset
browser, a selected record's typed inspector and the overlays.
``minneapolis`` opens the project written by build_minneapolis_btga.py.
``editor`` uses the ROM-free starter example (synthetic evidence).

Every run writes screenshots with adjacent evidence records and an
``index.json`` (``studio_evidence.EvidenceSession``): source revision, the
evidence class ("real-rom" or "synthetic") and source facts as hashes only.
Nothing is shown on screen (EGL off-screen rendering); no runtime is ever
launched.  The optional stub runtime only provides a capabilities file so
the enabled "Test in Game" state can be shown; its name says it is not a game.
"""

from __future__ import annotations

import argparse
import hashlib
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

from tools import studio_evidence as ev  # noqa: E402

REAL_MAPS = (("london", "LONDON - PARLIAMENT", "default", "dense urban map", "destructible"),
             ("crossfire", "PARIS - CROSSFIRE", "default", "multiplayer map (four multiplayer bases)", "multiplayer_structure"),
             ("white_house", "WASHINGTON DC - WHITE HOUSE", "default", "map with many objectives and collision", "objective"))


def _app(home: Path, catalog: Path | None = None, project: Path | None = None):
    os.environ["PYOPENGL_PLATFORM"] = "egl"
    os.environ["BTGA_STUDIO_HOME"] = str(home)
    from tools.btga_studio.core.settings import Settings
    from tools.btga_studio.gui.app import StudioApp
    from tools.btga_studio.gui.offscreen import OffscreenUI

    settings = Settings.open()
    prefs = settings.preferences
    prefs.welcome_seen, prefs.show_hub_at_start, prefs.tutorial_done, prefs.tutorial_active = True, False, True, False
    app = StudioApp(settings, catalog, project)
    ui = OffscreenUI(app, 1600, 960)
    app.show_hub = False
    return app, ui


def look(camera, eye, target) -> None:
    import numpy as np

    camera.position = np.asarray(eye, float)
    dx, dy, dz = (target[i] - eye[i] for i in range(3))
    camera.yaw = math.atan2(-dx, -dz)
    camera.pitch = math.atan2(dy, math.hypot(dx, dz))


def settle(ui, frames: int = 6) -> None:
    for _ in range(frames):
        ui.frame()
    time.sleep(0.2)  # thumbnails render in a worker; give them a moment and draw again
    for _ in range(3):
        ui.frame()


def oblique(app, scale: float = 0.55, height: float = 0.45) -> None:
    lo, hi = app.controller.document.scene.world_bounds()
    centre = (lo + hi) / 2
    span = float(max(hi[0] - lo[0], hi[2] - lo[2]))
    look(app.controller.camera, (centre[0], centre[1] + span * height, centre[2] + span * scale), (centre[0], centre[1], centre[2] - span * 0.05))


def layers(app, **values) -> None:
    app.controller.preferences.layers.update(values)


def errors_logged(home: Path) -> list[str]:
    log = home / "logs" / "studio.log"
    if not log.exists():
        return []
    return [line for line in log.read_text(encoding="utf-8").splitlines() if '"level": "error"' in line]


# ------------------------------------------------------------------- real maps --

def real_maps(output: Path, home: Path) -> dict:
    from tools.btga_studio.core.sources import SourceRegistry

    registry = SourceRegistry(home, home)
    sources = [s for s in registry.sources.values() if s.platform == "n64_btga" and s.catalog]
    if not sources:
        raise SystemExit("no Global Assault cartridge is registered with a built catalog in this Studio home")
    source = sources[0]
    catalog = registry._catalog_path(source.catalog)
    facts = {"rom_sha256": source.sha256, "rom_title": source.title, "source_status": source.status,
             "catalog_json_sha256": ev.sha256_file(catalog / "catalog.json"), "pipeline": "Studio Source Manager (SourceRegistry.register + build_catalog)"}
    session = ev.EvidenceSession(output, ROOT, "real-rom", facts)
    app, ui = _app(home)
    c = app.controller
    levels = {(level.name, level.variant): level for level in c.library.levels()}
    report = {"maps": {}}
    try:
        for key, name, variant, role, category in REAL_MAPS:
            level = levels[(name, variant)]
            c.add_imported_map(key, level.id, name.title())
            if app.renderer is None:
                app.attach_library()
            app.switch_map(key)
            app._load_into_renderer()
            scene = c.document.scene
            records = {family: sum(1 for _ in scene.records(family)) for family in ("instances", "markers", "collision")}
            models = {r["model"] for _, r in scene.records("instances")}
            textures = {m.get("texture") for model in list(models)[:400] for m in c.library.model(model).mesh["materials"] if m.get("texture")}
            base = scene.base
            report["maps"][key] = {
                "scene": level.id, "role": role, "records": records,
                "models_from_catalog": all(model in c.library._models and model.startswith("n64_btga:") for model in models),
                "textures_from_catalog": all(t in c.library._textures and t.startswith("n64_btga:") for t in textures if t),
                "minimap": (base.get("minimap") or {}).get("status"), "environment_records": len(base.get("environment") or []),
                "navigation": "decoded" if base.get("navigation") else "absent (the Global Assault adapter writes none)",
                "scene_source_rom_sha256": base["source"].get("rom_sha256")}
            layers(app, geometry=True, collision=False, markers=True, navigation=True, bounds=True, unresolved=False, grid=False)
            app.map_only, app.filter_platform, app.thumbnail_mode = True, "n64_btga", True  # the browser lists this map's game objects
            oblique(app, 0.42, 0.32)
            settle(ui, 8)
            settle(ui, 12)
            session.shoot(ui, f"{key}_overview", f"Original {name.title()} ({role}) opened from the Source Manager catalog",
                          "full Studio window; the original map's models and textures in the viewport (game-record models drawn at their "
                          "records, placement inferred); asset browser filtered to Global Assault objects with thumbnails")
            app.thumbnail_mode = False
            target = next((r for _, r in scene.records("markers") if r.get("category") == category and not r.get("disabled")), None)
            if target is not None:
                c.frame_record(target["id"])
                tx, ty, tz = target["translation"]
                look(c.camera, (tx + 650, ty + 520, tz + 700), (tx, ty + 60, tz))  # pulled back so the record's model and surroundings show
                layers(app, collision=True)
                settle(ui, 8)
                session.shoot(ui, f"{key}_inspector", f"A {category.replace('_', ' ')} record from the cartridge selected",
                              "typed fields with evidence classes in the Inspector; the record framed in the viewport")
            doc = c.document
            doc.selection = []
            layers(app, collision=True, markers=True, navigation=True, unresolved=True, bounds=True)
            c.frame_map()
            settle(ui, 8)
            session.shoot(ui, f"{key}_overlays", f"{name.title()} with collision, gameplay-record and navigation overlays",
                          "collision boxes, spawn/objective markers and map bounds drawn over the map; no navigation lines because "
                          "the catalog has no decoded navigation for Global Assault levels")
        report["errors_logged"] = errors_logged(home)
    finally:
        ui.close()
        c.shutdown()
    report["index"] = session.finish()
    (output / "real-maps-report.json").write_text(json.dumps(report, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    return report


# ----------------------------------------------------------------- Minneapolis --

def write_stub_runtime(directory: Path, features) -> Path:
    """A capabilities file next to a program that only exits: it lets Studio show its enabled state; it is never started."""

    directory.mkdir(parents=True, exist_ok=True)
    program = directory / "btga-contract-stub"
    program.write_text("#!/bin/sh\necho 'contract test stub: not a game' >&2\nexit 3\n", encoding="utf-8")
    program.chmod(0o755)
    from tools.btga_studio.core import runtime

    (directory / runtime.SIDECAR).write_text(json.dumps({
        "schema": runtime.CAPABILITIES_SCHEMA, "schema_version": 1,
        "runtime": {"name": "Studio contract test stub (not a game)", "version": "0.0.0"},
        "launch_request_versions": [1], "package_schema_versions": [1], "games": ["n64_btga"], "features": sorted(features),
        "bot_slots": 8, "teams": 4, "capture_modes": ["none", "screenshot", "video"]}, indent=1, sort_keys=True), encoding="utf-8")
    return program


def minneapolis(output: Path, build: Path, catalog: Path, rom: Path | None, stub: Path | None) -> dict:
    home = output.parent / (output.name + "-home")
    shutil.rmtree(home, ignore_errors=True)
    shutil.copytree(build / "studio-home", home)
    report = json.loads((build / "build-report.json").read_text(encoding="utf-8"))
    rom_hash = report["sources"][0]["rom_sha256"] if report.get("sources") else None
    facts = {"rom_sha256": rom_hash, "catalog_json_sha256": ev.sha256_file(catalog / "catalog.json"),
             "project_sha256": ev.sha256_file(build / "Minneapolis-Global-Assault.btgaproj"),
             "package_sha256": ev.sha256_file(build / "Minneapolis-Global-Assault.btgapkg")}
    session = ev.EvidenceSession(output, ROOT, "real-rom", facts)
    app, ui = _app(home, catalog, build / "Minneapolis-Global-Assault.btgaproj")
    c = app.controller
    records = report["records"]
    notes = {}
    try:
        settle(ui, 6)
        app.switch_map(next(iter(c.project.maps)))
        app._load_into_renderer()
        layers(app, geometry=True, collision=False, markers=True, navigation=False, bounds=False, unresolved=False, grid=False)
        doc = c.document
        plaza = doc.record(records["plaza"])
        px, py, pz = plaza["translation"]
        look(c.camera, (px + 520, py + 640, pz + 980), (px, py, pz))
        settle(ui, 10)
        session.shoot(ui, "commons_square", "Commons square with Global Assault barriers, a capture bot tank and the capture zone",
                      "game tank traps and concrete barriers ring the square; the game tank of the capture bot; the zone ring")
        base_id, queen_id = records["bases"]["0"], records["queenlords"]["0"]
        base = doc.record(base_id)
        bx, by, bz = base["translation"]
        doc.selection = [queen_id]
        look(c.camera, (bx + 500, by + 330, bz + 520), (bx + 80, by + 40, bz + 80))
        settle(ui, 10)
        session.shoot(ui, "team_base_queenlord", "Team 0 base (game multiplayer base model) with its Queenlord bot selected",
                      "the game's base model and the Queenlord portrait card in the viewport; the Bot inspector shows kind Queenlord, "
                      "the model with its 'inferred' identification, team, behaviour and the defended base")
        boss = records["bots"]["boss"]
        doc.selection = [boss]
        c.frame_record(boss)
        settle(ui, 10)
        session.shoot(ui, "boss_tank", "A boss tank bot drawn with the game's tank",
                      "hull and turret of the game's tank at the bot; Bot inspector with behaviour 'boss'")
        tower = next(t for t in records["targets"] if doc.record(t)["fields"]["label"]["value"] == "Watchtower")
        c.frame_record(tower)
        wx, wy, wz = doc.record(tower)["translation"]
        look(c.camera, (wx + 700, wy + 520, wz + 760), (wx, wy + 120, wz))
        settle(ui, 10)
        session.shoot(ui, "destructible_prop", "A destructible target drawn with the game's watchtower",
                      "the game's watchtower model; Destructible target inspector lists intact and wreck models")
        pickup = next(r for _, r in doc.scene.records("markers") if r.get("category") == "item_pickup")
        c.frame_record(pickup["id"])
        kx, ky, kz = pickup["translation"]
        app.gizmo_operation = "select"  # no move handles over the small crate
        look(c.camera, (kx + 90, ky + 70, kz + 110), (kx, ky + 24, kz))
        settle(ui, 10)
        session.shoot(ui, "item_pickup", "An item pickup drawn with a game crate",
                      "the game's red-cross crate at the pickup; the Item pickup inspector names the item it gives")
        app.show_items, app.item_key = True, pickup["fields"]["item"]["value"]
        settle(ui, 6)
        app.gizmo_operation = "translate"
        session.shoot(ui, "item_editor", "The Items window for the pickup's item",
                      "the item editor with category, the game crate as model, icon, duration, stacking and effects")
        app.show_items = False
        app.open_scenarios("defend_the_queenlord")
        app.scenario_element = ("objectives", "obj1")
        settle(ui, 8)
        session.shoot(ui, "scenario_graph", "Scenario graph for 'Defend the Queenlord' on Minneapolis",
                      "objective boxes (capture, destroy, boss, optional collect/defend) with requires arrows, START and VICTORY; "
                      "objective editor with map-record pickers by name")
        app.show_scenarios = False
        import numpy as np

        from tools.btga_studio.core.navgraph import NavGraph

        graph = NavGraph.from_scene(doc.scene)  # start on a street navigation point, facing along the street to the next one
        node = next(n for n in sorted(graph.nodes) if graph.links.get(n))
        (ax, az), (bx_, bz_) = graph.nodes[node], graph.nodes[graph.links[node][0]]
        start = np.array([ax, doc.record(node)["translation"][1], az])
        app.drive_start_point = lambda: (start, math.atan2(-(bx_ - ax), -(bz_ - az)))
        doc.selection = []
        app.start_drive_preview()
        app.drive_input = (1.0, 0.0, False)
        for _ in range(70):
            ui.frame()
        app.drive_input = (0.0, 0.0, True)
        settle(ui, 4)
        session.shoot(ui, "drive_preview", "Drive Preview with the game's tank on Minneapolis",
                      "the 'editor approximation' banner, the disclaimer about physics/AI/combat/destruction/pickups/scripting, "
                      "the game's tank, collision outlines and navigation reach overlays")
        app.stop_drive_preview()
        settle(ui, 2)
        if stub is not None and rom is not None:
            from tools.btga_studio.core import runtime

            program = write_stub_runtime(stub, runtime.FEATURES)
            prefs = app.settings.preferences
            prefs.runtime_executable, prefs.runtime_rom = None, None
            app._runtime_cache = (None, None, None)
            app.open_test_in_game()
            settle(ui, 4)
            session.shoot(ui, "test_in_game_unavailable", "Test in Game with no runtime configured",
                          "the toolbar's Test in Game button is disabled and the window names RUNTIME_ABSENT")
            app.show_test_in_game = False
            prefs.runtime_executable, prefs.runtime_rom = str(program), str(rom)
            app._runtime_cache = (None, None, None)
            done = []
            c.export_package(output.parent / (output.name + "-package.btgapkg"), "lab", lambda job: done.append(job))
            for _ in range(600):
                ui.frame()
                if done:
                    break
                time.sleep(0.2)
            app.open_test_in_game()
            app.launch["scenario"] = "defend_the_queenlord"
            app.launch["spawn"] = c.project.scenarios["defend_the_queenlord"]["start"]["player_spawn"]
            app.launch["bots"] = {4, 5, 6, 7}
            app.check_launch()
            settle(ui, 4)
            session.shoot(ui, "test_in_game_ready", "Test in Game with a compatible runtime declared (contract test stub, never started)",
                          "the toolbar's Test in Game button enabled; the window lists runtime 'Studio contract test stub (not a game)', "
                          "the package and 'Ready: every check passed'; Launch was not pressed")
            notes["launch_plan_ok"] = bool(app.launch_plan and app.launch_plan.ok)
            notes["launch_problems"] = list(app.launch_plan.problems) if app.launch_plan else None
            prefs.runtime_executable, prefs.runtime_rom = None, None
            app.show_test_in_game = False
        notes["errors_logged"] = errors_logged(home)
    finally:
        ui.close()
        c.shutdown()
    index = session.finish()
    result = {"index": index, **notes}
    (output / "minneapolis-report.json").write_text(json.dumps(result, indent=1, sort_keys=True, default=str) + "\n", encoding="utf-8")
    return result


# ---------------------------------------------------------------------- editor --

def editor(output: Path) -> dict:
    home = output.parent / (output.name + "-home")
    shutil.rmtree(home, ignore_errors=True)
    session = ev.EvidenceSession(output, ROOT, "synthetic", {"content": "Studio starter example (no game data)"})
    app, ui = _app(home, None, ROOT / "examples" / "starter-example.btgaproj")
    c = app.controller
    notes = {}
    try:
        settle(ui, 6)
        app.switch_map("training_grounds")
        app._load_into_renderer()
        layers(app, collision=True, markers=True, navigation=True, grid=False)
        app.open_scenarios("first_strike")
        app.scenario_element = ("objectives", "obj2")
        settle(ui, 8)
        session.shoot(ui, "scenario_graph", "Scenario editor: objective graph of the starter example",
                      "START, ordered objectives with requires arrows, a dashed optional objective, VICTORY; objective editor on the right")
        app.scenario_change("Add a looping requirement for the demonstration", lambda: c.project.scenarios["first_strike"]["objectives"]["obj1"].__setitem__(
            "requires", ["obj3"]))
        app.scenario_element = ("objectives", "obj1")
        settle(ui, 6)
        session.shoot(ui, "scenario_validation", "Scenario editor showing a forbidden requirement loop",
                      "red-outlined objective boxes and the OBJECTIVE_CYCLE message naming the loop")
        app.undo()
        for tab, name, element in ((1, "scenario_triggers", ("triggers", "trg1")), (4, "scenario_start_end", None), (7, "scenario_check", None)):
            app.scenario_element = element
            app.scenario_tab = tab
            from tools.btga_studio.gui import authoring as authoring_gui

            # open a tab by drawing it selected once: imgui tab bars follow clicks, so select via the label anchor
            app._force_tab = authoring_gui.SCENARIO_TABS[tab]
            settle(ui, 6)
            session.shoot(ui, name, f"Scenario editor tab '{authoring_gui.SCENARIO_TABS[tab]}'",
                          "graphical editors (pickers by name, condition lists, actions) and no raw JSON or internal IDs")
        app._force_tab = None
        app.show_scenarios = False
        bot = [r["id"] for _, r in c.document.scene.records("markers") if r.get("category") == "bot_slot"][1]
        c.frame_record(bot)
        bx, by, bz = c.document.record(bot)["translation"]
        look(c.camera, (bx + 700, by + 900, bz + 1100), (bx, by, bz + 500))  # pulled back: the patrol route to the north is in view
        settle(ui, 6)
        session.shoot(ui, "bot_inspector", "A patrol bot selected on the starter example",
                      "Bot inspector: kind, model, team, behaviour, patrol start and weapons by name; the patrol route drawn in the viewport")
        app.show_items, app.item_key = True, "speed_boost"
        settle(ui, 6)
        session.shoot(ui, "item_editor", "Items window: a temporary speed modifier",
                      "category, model, icon, duration, stacking, max stack and an effect list using the declarative actions")
        app.show_items = False
        spawn = next(r for _, r in c.document.scene.records("markers") if r.get("category") == "player_spawn")
        import numpy as np

        app.drive_start_point = lambda: (np.asarray(spawn["translation"], float), 0.0)  # facing north, towards the gate and the hill
        app.start_drive_preview()
        app.drive_input = (1.0, 0.0, False)
        for _ in range(55):
            ui.frame()
        settle(ui, 2)
        session.shoot(ui, "drive_preview_overlays", "Drive Preview on the starter example",
                      "the approximation banner, trigger-volume and zone rings, spawn and bot arrows, navigation reach colours, collision")
        app.stop_drive_preview()
        notes["errors_logged"] = errors_logged(home)
    finally:
        ui.close()
        c.shutdown()
    index = session.finish()
    (output / "editor-report.json").write_text(json.dumps({"index": index, **notes}, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    return {"index": index, **notes}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("command", choices=("real-maps", "minneapolis", "editor"))
    parser.add_argument("output", type=Path)
    parser.add_argument("--home", type=Path)
    parser.add_argument("--build", type=Path)
    parser.add_argument("--catalog", type=Path)
    parser.add_argument("--rom", type=Path)
    parser.add_argument("--stub-runtime", type=Path)
    args = parser.parse_args(argv)
    output = args.output.resolve()
    if output.exists():
        shutil.rmtree(output)
    if args.command == "real-maps":
        result = real_maps(output, args.home.resolve())
    elif args.command == "minneapolis":
        result = minneapolis(output, args.build.resolve(), args.catalog.resolve(), args.rom, args.stub_runtime)
    else:
        result = editor(output)
    print(json.dumps({"shots": [s["file"] for s in result["index"]["shots"]], "stale": result["index"]["stale"]}, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
