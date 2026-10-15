"""Capture the product screenshots (welcome, hub, wizard, Source Manager, editors, dialogs) off-screen.

Application framebuffer only (EGL, no window, no desktop).  Synthetic data
only: Studio starter content, and a synthetic ROM that ``tools.btga_studio.fixtures``
temporarily treats as a supported revision.  Each image is checked
automatically (blank or flat images fail) and gets an adjacent JSON record
tied to the source identity; ``--review`` merges reviewer verdicts (see
``capture_studio_acceptance.py``).

    python tools/capture_studio_product.py OUTPUT_DIR
    python tools/capture_studio_product.py OUTPUT_DIR --review VERDICTS.json
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools import studio_evidence as ev  # noqa: E402
from tools.capture_studio_acceptance import merge_review  # noqa: E402


def capture(output: Path) -> list[dict]:
    os.environ.setdefault("PYOPENGL_PLATFORM", "egl")
    import numpy as np

    from tools.btga_studio.core import geometry as g
    from tools.btga_studio.core import hub
    from tools.btga_studio.core.settings import Settings
    from tools.btga_studio.fixtures import (ScriptedDialogs, recording_launcher, simulated_catalog_builder, supported_revision,
                                            synthetic_rom)
    from tools.btga_studio.gui.app import StudioApp
    from tools.btga_studio.gui.offscreen import OffscreenUI

    identity = ev.source_identity(ROOT)
    output.mkdir(parents=True, exist_ok=True)
    results = []
    with tempfile.TemporaryDirectory(prefix="product capture ") as temp, recording_launcher():
        home = Path(temp) / "home"
        os.environ["BTGA_STUDIO_HOME"] = str(home)
        app = StudioApp(Settings.open())
        ui = OffscreenUI(app, 1600, 960)
        c = app.controller

        def shoot(name, scenario, expected, settle=4):
            for _ in range(settle):
                ui.frame()
            width, height, rgba = ui.pixels()
            stats = ev.image_stats(width, height, rgba)
            problems = ev.inspect_image(stats)
            png = output / f"{name}.png"
            ui.screenshot(png)
            record = ev.write_record(png, identity, scenario, expected, f"automatic: {stats['unique_colours']} colours", not problems,
                                     "automatic checks only; pending visual review" if not problems else "; ".join(problems), stats=stats)
            results.append({"name": name, "ok": not problems, "problems": problems, "record": record.name})

        def top_down(height):
            c.camera = g.Camera(position=np.array([0.0, height, 0.0]), pitch=-(math.pi / 2 - 1e-6))

        try:
            shoot("01_first_launch_welcome", "first launch with no projects or games",
                  "welcome window with Create Original / Import From My Games / Open Existing / Open Example and help links")

            projects_dir = app.settings.projects_directory
            made = []
            for title, template in (("Desert Raid", "mission"), ("Arena Night", "skirmish"), ("Sandbox", "blank")):
                path, _ = hub.create(c.library, title, template, 1024.0, 4, "deathmatch", projects_dir)
                made.append(path)
                app.settings.preferences.add_recent(str(path))
            moved = projects_dir / "Old Map.btgaproj"
            app.settings.preferences.add_recent(str(moved))  # never existed: shows the moved/deleted state
            app.settings.preferences.set_pinned(str(made[1]), True)
            app.settings.preferences.welcome_seen = True
            app.open_hub()
            shoot("02_project_hub", "Project Hub with recent, pinned and moved projects",
                  "recent list with Ready / moved states, a pinned entry, Locate and Pin actions")

            app.show_hub = False
            app.open_wizard()
            app.wizard_state.update({"title": "Canyon Clash", "template": 1, "size": 1, "players": 3})
            shoot("03_new_project_wizard", "new-project wizard", "name, template choice, map size, players and mode, save location")
            app.show_wizard = False

            app.show_sources = True
            shoot("04_source_manager_empty", "Source Manager with no games", "explanation that files stay local, add buttons, empty-state guidance")

            rom = synthetic_rom(42)
            roms = Path(temp) / "my games"
            roms.mkdir()
            good, odd = roms / "Global Assault.z64", roms / "Unknown Game.z64"
            good.write_bytes(rom)
            odd.write_bytes(synthetic_rom(43))
            with supported_revision(rom), simulated_catalog_builder(delay=0.25, steps=12):
                app.check_source(good)
                app.check_source(odd)
                c.jobs.wait()
                shoot("05_source_identification", "two files checked: a recognised (synthetic) copy and an unrecognised one",
                      "game, platform, region, byte order, revision and hash for each; plain-language status; Add to Studio for the good one")
                c.add_source_async(good, app._registered)
                app.identified.clear()
                c.jobs.wait()
                source_id = next(iter(c.registry.sources))
                c.build_catalog(source_id, on_done=app._built)
                time.sleep(0.8)
                shoot("06_import_progress", "catalog build in progress", "progress bar with stage and count and a Cancel button", settle=2)
                c.jobs.wait(60)
                ui.frame()
                second = roms / "copy to move.z64"
                second.write_bytes(rom)
            shoot("07_registered_source_health", "registered source with a built catalog", "Ready health, catalog Ready with size, Rebuild/Relink/Remove")
            app.show_sources = False

            app.open_project(made[0])
            c.frame_map()
            shoot("08_main_editor_layout", "editor with a mission project open",
                  "toolbar, project/gameplay/layers panel, viewport, inspector, minimap, assets, output and status bar")

            app.thumbnail_mode, app.map_only, app.search = True, False, "starter:"
            for _ in range(40):
                ui.frame()
                if app.thumbnails:
                    app.thumbnails.pump()
            shoot("09_asset_browser_thumbnails", "asset browser in thumbnail mode", "thumbnails of the starter models")
            app.thumbnail_mode, app.search = False, ""

            models = c.library.starter()["models"]
            app.try_placement("model", models["building_small"])
            c.camera = g.Camera(position=np.array([-700.0, 650.0, 900.0]), yaw=math.radians(-35.0), pitch=math.radians(-30.0))
            for _ in range(2):
                ui.frame()
            screen = ui.viewport_to_screen(c.viewport[0] * 0.55, c.viewport[1] * 0.6)
            for _ in range(3):
                ui.frame(mouse=screen)
            shoot("10_viewport_placement", "placing a building", "placement preview box under the cursor and PLACING status", settle=1)
            ui.click(screen)
            ui.press("escape", mouse=screen)
            placed = c.document.selection[0] if c.document.selection else None
            for tool, name in (("translate", "11a_tool_move"), ("rotate", "11b_tool_rotate"), ("scale", "11c_tool_scale")):
                app.gizmo_operation = tool
                if placed:
                    c.document.selection = [placed]
                shoot(name, f"{tool} tool on the placed building", f"{tool} handles; toolbar shows the active tool; status bar names it")

            app.gizmo_operation = "translate"
            c.document.selection = []
            top_down(1800.0)
            for tab, name, scenario in (("Spawns & teams", "12_spawn_editor", "spawn and team list"),
                                        ("Objectives & navigation", "13_objective_navigation_editor", "objectives and navigation links"),
                                        ("Missions & campaigns", "14_mission_campaign_editor", "campaign order and mission objectives"),
                                        ("Minimap", "15_minimap_editor", "generated minimap settings")):
                app.open_gameplay(tab)
                ui.frame()
                shoot(name, f"gameplay editor: {scenario}", f"{tab} tab with useful controls")
            app.show_gameplay = False

            objective = next(r["id"] for f, r in c.document.scene.records("markers") if r.get("category") == "objective")
            target = c.document.record(objective)["fields"]["target"]["value"]
            c.document.delete([target])
            c.request_diagnostics(immediate=True)
            c.poll_diagnostics(wait=True)
            app.export_package()
            ui.frame()
            for label, action in app.errors[0]["actions"]:
                if label == "Show the first problem":
                    action()
            ui.frame()
            shoot("16_validation_error_focus", "export blocked by a dangling objective target",
                  "dialog lists the problem with map, object and field; the objective is selected in the viewport")
            app.errors.clear()
            c.document.undo()
            c.request_diagnostics(immediate=True)
            c.poll_diagnostics(wait=True)

            app.dialogs = ScriptedDialogs(projects_dir / "Desert Raid.btgapkg")
            app.export_package()
            c.jobs.wait()
            shoot("17_export_success", "content package exported", "success dialog with name, size, maps and Show in folder")
            app.export_result = None

            app.show_about = True
            shoot("18_about_dialog", "About dialog", "version and build information, licence pointer")
            app.show_about = False
            ui.frame()

            app.show_settings = True
            shoot("19_settings_cache_logs", "settings with data locations and maintenance", "locations with Open buttons, logs, cache, layout reset, releases")
            app.show_settings = False

            broken = projects_dir / "Damaged Project.btgaproj"
            broken.write_text('{"schema": "btga-studio-project", "schema_version": 2, "maps": [1]}', encoding="utf-8")
            app.open_project(broken)
            shoot("20_malformed_project_recovery", "opening a damaged project file", "plain-language error, Remove from recent list, app still usable behind it")
        finally:
            ui.close()
    return results


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("output", type=Path)
    parser.add_argument("--review", type=Path)
    args = parser.parse_args(argv)
    if args.review:
        merge_review(args.output, args.review)
        return 0
    results = capture(args.output)
    print(json.dumps(results, indent=1))
    return 0 if all(r["ok"] for r in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
