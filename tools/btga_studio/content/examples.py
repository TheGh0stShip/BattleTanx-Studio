"""ROM-free example project built only from Studio starter content.

It contains a small mission map with a one-mission campaign, a
multiplayer arena, a target-range minigame, placeholder tanks and two gangs.
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
    project.define("missions", "first_strike", {"title": "First strike", "map": "training_grounds", "objectives": [objective],
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
