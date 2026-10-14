"""Capture application-only acceptance screenshots of BattleTanx Studio off-screen (EGL; no window, no desktop).

Uses only ROM-free starter content.  Every image is read back from the
editor's own framebuffer, checked automatically (blank or flat images are
rejected) and written with an adjacent JSON evidence record carrying the
source identity.  Human review remains required; see ``--review``.

    python tools/capture_studio_acceptance.py OUTPUT_DIR
    python tools/capture_studio_acceptance.py OUTPUT_DIR --review REVIEW.json   # merge reviewer verdicts
"""

from __future__ import annotations

import argparse
import json
import math
import os
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools import studio_evidence as ev  # noqa: E402


def _top_down(height: float):
    import numpy as np

    from tools.btga_studio.core import geometry as g

    return g.Camera(position=np.array([0.0, height, 0.0]), pitch=-(math.pi / 2 - 1e-6))


def capture(output: Path) -> list[dict]:
    os.environ.setdefault("PYOPENGL_PLATFORM", "egl")
    import numpy as np

    from tools.btga_studio.content.examples import build_example
    from tools.btga_studio.core import geometry as g
    from tools.btga_studio.core.settings import Settings
    from tools.btga_studio.gui.app import StudioApp
    from tools.btga_studio.gui.offscreen import OffscreenUI

    identity = ev.source_identity(ROOT)
    output.mkdir(parents=True, exist_ok=True)
    results = []
    with tempfile.TemporaryDirectory() as temp:
        os.environ["BTGA_STUDIO_HOME"] = str(Path(temp) / "home")
        app = StudioApp(Settings.open())
        ui = OffscreenUI(app, 1600, 960)
        c = app.controller
        try:
            def shoot(name, scenario, expected, region="viewport", settle=4):
                for _ in range(settle):
                    ui.frame()
                width, height, rgba = ui.pixels()
                if region == "viewport":
                    ox, oy = app.viewport_origin
                    box = (ox, oy, c.viewport[0], c.viewport[1])
                elif region == "full":
                    box = None
                else:
                    box = tuple(v * s for v, s in zip(region, (width, height, width, height)))
                stats = ev.image_stats(width, height, rgba, box)
                problems = ev.inspect_image(stats)
                png = output / f"{name}.png"
                ui.screenshot(png)
                record = ev.write_record(png, identity, scenario, expected,
                                         f"automatic: {stats['unique_colours']} colours, dominant {stats['dominant_fraction']:.1%}",
                                         not problems, "automatic checks only; pending visual review" if not problems else "; ".join(problems),
                                         region=[round(v, 1) for v in box] if box else None, stats=stats)
                results.append({"name": name, "ok": not problems, "problems": problems, "record": record.name})

            # Project overview: the ROM-free example opened through the wizard's example action.
            app.open_example()
            c.switch_map("training_grounds")
            app._load_into_renderer()
            shoot("01_project_overview", "starter example project open; maps, definitions and layers listed",
                  "hierarchy lists three maps and definitions; viewport shows the training grounds", region="full")

            app.thumbnail_mode = True
            app.map_only = False
            app.search = "starter:"  # catalog models from the starter library (placed instances are listed separately)
            for _ in range(40):  # thumbnails render in worker processes
                ui.frame()
                if app.thumbnails:
                    app.thumbnails.pump()
            shoot("02_asset_browser", "asset browser with original starter models as thumbnails",
                  "Assets panel shows starter models (buildings, wall, ramp, road, tiles)", region=(0.0, 0.72, 0.6, 0.28))

            c.camera = g.Camera(position=np.array([-900.0, 520.0, 1150.0]), yaw=math.radians(-35.0), pitch=math.radians(-22.0))
            shoot("03_textured_freefly_viewport", "free-fly perspective camera over textured starter geometry",
                  "textured tiles, road, buildings, wall and ramp in perspective")

            document = c.document
            building = next(r["id"] for f, r in document.scene.records("instances")
                            if r["model"] == c.library.starter()["models"]["building_tall"])
            app.search = ""
            app.thumbnail_mode = False
            c.camera = g.Camera(position=np.array([450.0, 420.0, 150.0]), yaw=math.radians(35.0), pitch=math.radians(-20.0))
            document.selection = [building]
            for mode, label in (("translate", "04_gizmo_translate"), ("rotate", "05_gizmo_rotate"), ("scale", "06_gizmo_scale")):
                app.gizmo_operation = mode
                shoot(label, f"selected building with the {mode} gizmo", f"{mode} manipulator drawn on the selected building")

            # Overlays from a top-down view.
            c.camera = _top_down(2600.0)
            document.selection = []
            layers = c.preferences.layers
            saved_layers = dict(layers)
            box = {"translation": [-700.0, 0.0, 300.0], "yaw": 0, "min": [-120.0, 0.0, -60.0], "max": [120.0, 80.0, 60.0],
                   "vertical": {"status": "proven", "evidence": "synthetic acceptance fixture"}, "category": "box", "kind": "box"}
            document._commit("Add collision (synthetic fixture)", [{"op": "add_collision", "collision": box}], set())
            for name, only, scenario in (("07_collision_overlay", "collision", "collision overlay (one synthetic box)"),
                                         ("08_spawn_overlay", "markers", "spawn and gameplay marker overlay"),
                                         ("10_navigation_overlay", "navigation", "navigation overlay with authored node links"),
                                         ("11_map_bounds", "bounds", "map bounds overlay"),
                                         ("12_editor_grid", "grid", "editor grid and axes")):
                for key in ("collision", "markers", "navigation", "bounds", "grid"):
                    layers[key] = key == only
                layers["geometry"] = True
                if name == "08_spawn_overlay":
                    shoot(name, scenario, "marker boxes with facing ticks at the spawn, spawner, pickup, hazard and objective")
                    continue
                shoot(name, scenario, f"only the {only} overlay is drawn over geometry")
            objective = next(r["id"] for f, r in document.scene.records("markers") if r.get("category") == "objective")
            layers.update({"markers": True, "collision": False, "navigation": False, "bounds": False, "grid": False})
            document.selection = [objective]
            shoot("09_objective_overlay", "objective marker selected with its inspector", "objective marker highlighted; inspector shows typed fields",
                  region="full")
            layers.update(saved_layers)

            document.selection = []
            shoot("13_minimap", "minimap panel for the open map", "generated overhead minimap with camera position",
                  region=(0.76, 0.48, 0.24, 0.24))

            pickup = next(r["id"] for f, r in document.scene.records("markers") if r.get("category") == "pickup")
            document.selection = [pickup]
            shoot("14_typed_inspector", "typed inspector for a pickup", "integer/enum controls with ranges, read-only rows marked",
                  region=(0.76, 0.03, 0.24, 0.45))

            # Validation error focused on the responsible object: delete the objective's target so its field dangles.
            target = document.record(objective)["fields"]["target"]["value"]
            document.selection = []
            document.delete([target])
            c.request_diagnostics(immediate=True)
            c.poll_diagnostics(wait=True)
            focus = next(d for d in c.diagnostics if d.code == "DANGLING_REFERENCE" and d.target == objective and d.field == "target")
            c.frame_record(focus.target)  # what clicking the diagnostic row does
            shoot("15_validation_error_focus", "objective whose target was deleted: DANGLING_REFERENCE on objective.target, row clicked",
                  "Validation tab lists DANGLING_REFERENCE naming the objective and field target; the objective is selected and framed",
                  region="full")
            document.undo()
            document.selection = []

            # Save, close and reopen.
            path = Path(temp) / "saved project ü" / "acceptance example.btgaproj"
            c.save(path)
            c.close_map()
            c.project = None
            ui.frame()
            app.open_project(path)
            c.camera = _top_down(2600.0)
            shoot("16_reopened_project", "project reopened from disk (path with spaces and non-ASCII)",
                  "same maps and records as before saving; title has no unsaved marker", region="full")

            # Blank map built in this session, with its generated minimap.
            c.add_blank_map("demo_map", "ROM-free demo", 1536.0)
            app._load_into_renderer()
            models = c.library.starter()["models"]
            templates = {t["source_kind"].removeprefix("studio_"): t for t in c.library.starter()["templates"]}
            demo = c.document
            for x in range(-1536, 1536, 512):
                for z in range(-1536, 1536, 512):
                    demo.place_model(models["ground_tile_concrete" if (x // 512 + z // 512) % 2 else "ground_tile"], (x + 256.0, 0.0, z + 256.0))
            demo.place_model(models["road_straight"], (0.0, 0.0, 0.0))
            demo.place_model(models["road_straight"], (0.0, 0.0, 512.0))
            for x, z, kind in ((-600.0, -600.0, "building_tall"), (600.0, -600.0, "building_small"), (-600.0, 600.0, "building_small")):
                demo.place_model(models[kind], (x, 0.0, z))
            demo.place_model(models["wall"], (300.0, 0.0, 300.0), 0x4000)
            demo.place_model(models["ramp"], (-300.0, 0.0, -200.0))
            demo.place_marker(templates["player_spawn"], (0.0, 0.0, 1200.0), 0x8000)
            demo.place_marker(templates["pickup"], (600.0, 0.0, 600.0))
            demo.place_marker(templates["hazard"], (-300.0, 0.0, 300.0))
            target = demo.place_model(models["cube"], (0.0, 0.0, -1100.0))
            demo.place_marker(templates["objective"], (0.0, 0.0, -1000.0), values={"target": target, "label": "Reach the depot"})
            nodes = [demo.place_marker(templates["nav_node"], (0.0, 0.0, z)) for z in (900.0, 0.0, -900.0)]
            demo.set_field(nodes[0], "link_0", nodes[1])
            demo.set_field(nodes[1], "link_0", nodes[2])
            demo.selection = []
            c.camera = _top_down(3000.0)
            shoot("17_generated_minimap", "generated minimap of a new blank map", "overhead render of the demo map in the Minimap panel",
                  region=(0.76, 0.48, 0.24, 0.24))
            c.camera = g.Camera(position=np.array([-1500.0, 900.0, 1700.0]), yaw=math.radians(-40.0), pitch=math.radians(-25.0))
            shoot("18_final_rom_free_map", "final ROM-free example map in perspective", "complete demo map built only from starter content",
                  region="full")
        finally:
            ui.close()
    return results


def merge_review(output: Path, review: Path) -> None:
    """Apply reviewer verdicts {name: {"result", "observed", "notes"}}; failed images are moved to rejected/."""

    verdicts = json.loads(review.read_text(encoding="utf-8"))
    rejected = output / "rejected"
    for name, verdict in sorted(verdicts.items()):
        record_path = output / f"{name}.json"
        record = json.loads(record_path.read_text(encoding="utf-8"))
        if record["sha256"] != ev.sha256_file(output / f"{name}.png"):
            raise SystemExit(f"{name}: image changed since capture")
        record.update({"result": verdict["result"], "observed": verdict["observed"], "reviewer_notes": verdict["notes"]})
        record_path.write_text(json.dumps(record, indent=1, sort_keys=True) + "\n", encoding="utf-8")
        if verdict["result"] != "pass":
            rejected.mkdir(exist_ok=True)
            for suffix in (".png", ".json"):
                shutil.move(str(output / f"{name}{suffix}"), str(rejected / f"{name}{suffix}"))


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("output", type=Path)
    parser.add_argument("--review", type=Path, help="merge reviewer verdicts instead of capturing")
    args = parser.parse_args(argv)
    if args.review:
        merge_review(args.output, args.review)
        return 0
    results = capture(args.output)
    print(json.dumps(results, indent=1))
    return 0 if all(r["ok"] for r in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
