"""Synthetic sample catalog and project for BTGA Studio.

Everything here is generated procedurally (checker textures, boxes,
planes); no game data is used.  The sample feeds tests, screenshots and
the packaged demo.

    python3 -m tools.btga_studio.samples OUTPUT_DIR
"""

from __future__ import annotations

import argparse
import struct
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.btga_assets import export as ex  # noqa: E402
from tools.btga_assets import mesh as meshmod  # noqa: E402
from tools.btga_assets.semantics import SCENE_SCHEMA_VERSION, collision, field, marker  # noqa: E402

SAMPLE_SHA = "5a" * 32


def checker(colour_a, colour_b, size: int = 16, cell: int = 4) -> dict:
    rgba = bytearray()
    for y in range(size):
        for x in range(size):
            rgba += bytes(colour_a if ((x // cell) + (y // cell)) % 2 == 0 else colour_b)
    return {"width": size, "height": size, "rgba": bytes(rgba), "wrap": {"s": "repeat", "t": "repeat"}, "alpha": "opaque", "format": "rgba8"}


def box_mesh(lo, hi, texture: str | None, uv_scale: float = 1.0) -> dict:
    builder = meshmod.MeshBuilder()
    (x0, y0, z0), (x1, y1, z1) = lo, hi
    corners = [(x, y, z) for x in (x0, x1) for y in (y0, y1) for z in (z0, z1)]
    for corner in corners:
        builder.position(corner)
    material = builder.material({"id": f"{texture}|box", "double_sided": False, "alpha_mode": "OPAQUE", "texture": texture})
    faces = [(0, 1, 3, 2), (4, 6, 7, 5), (0, 4, 5, 1), (2, 3, 7, 6), (0, 2, 6, 4), (1, 5, 7, 3)]
    uv = [(0, 0), (uv_scale, 0), (uv_scale, uv_scale), (0, uv_scale)]
    for face in faces:
        for tri in ((0, 1, 2), (0, 2, 3)):
            builder.triangle(material, [{"p": face[i], "uv": list(uv[i]), "colour": None, "normal": None} for i in tri])
    return builder.build()


def plane_mesh(half: float, texture: str) -> dict:
    builder = meshmod.MeshBuilder()
    for p in ((-half, 0, -half), (half, 0, -half), (half, 0, half), (-half, 0, half)):
        builder.position(p)
    material = builder.material({"id": f"{texture}|ground", "double_sided": True, "alpha_mode": "OPAQUE", "texture": texture})
    uv = [(0, 0), (40, 0), (40, 40), (0, 40)]
    for tri in ((0, 2, 1), (0, 3, 2)):
        builder.triangle(material, [{"p": i, "uv": list(uv[i]), "colour": None, "normal": None} for i in tri])
    return builder.build()


def model_record(model_id: str, platform: str, mesh: dict, library: str | None = None) -> dict:
    source = {"platform": platform, "synthetic": True}
    if library:
        source["library"] = library
    return {"id": model_id, "schema": "btga-neutral", "schema_version": 1, "kind": "model", "source": source, "mesh": mesh, "bounds": meshmod.bounds(mesh)}


def sample_scene(platform: str, name: str, models: dict[str, str], planar: bool) -> dict:
    p = platform
    team = field(1, "u8", 3, "proven", "owner slot", legal=(0, 7))
    spawner_fields = {
        "owner_slot": team,
        "spawn_budget": field(10, "u16", 4, "proven", "units left to spawn", sentinel={0: "disabled", 0xFFFF: "unlimited"}),
        "unit_type_mask": field(0b101, "u16", 6, "proven", "unit types 0..14", legal=(1, 0x7FFF), bits=15),
        "spawn_interval": field(300, "u32", 8, "proven", "ticks between spawns"),
        "spawn_mode": field(1, "u8", 1, "proven", "spawn mode", enum={1: "throttled", 2: "every check", 3: "every check, flagged", 4: "player flag"}),
    }
    markers = [
        marker(f"{p}:marker:spawner", p, "ga_kind_13" if p != "ps1" else "ps1_kind_13", "unit_spawner", [-600, 0, -600], 0, spawner_fields,
               source={"synthetic": True}, evidence=["synthetic sample"], confidence="proven", raw=bytes(15), restrictions={}),
    ]
    start_kind = {"n64_btga": ("ga_kind_31", "multiplayer_structure"), "n64_bt1": ("bt1_marker_25", "team_spawn_slot")}.get(p)
    if start_kind:  # PS1 spawn kinds are not traced, so the PS1 sample has none
        for name, x, yaw, team_value in (("start_a", -900, 0x4000, 0), ("start_b", 900, 0xC000, 1)):
            markers.append(marker(f"{p}:marker:{name}", p, start_kind[0], start_kind[1], [x, 0, 800], yaw,
                                  {"team": field(team_value, "u8", 2, "proven", "team", legal=(0, 15))}, source={"synthetic": True},
                                  evidence=["synthetic sample"], confidence="proven", raw=bytes(28), restrictions={"max_per_team": 5}))
    markers += [
        marker(f"{p}:marker:pickup", p, "ga_kind_16", "pickup", [0, 0, 300], 0,
               {"pickup_type": field(2, "u8", 3, "proven", "pickup type"), "half_width_x": field(30, "u8", 1, "proven", "box half-width x"),
                "half_width_z": field(30, "u8", 2, "proven", "box half-width z")},
               source={"synthetic": True}, evidence=["synthetic sample"], confidence="proven", raw=bytes(4)),
        marker(f"{p}:marker:objective", p, "ga_kind_21", "objective", [500, 0, -500], 0,
               {"objective_class": field(1, "u8", 20, "proven", "objective class", enum={0: "not counted", 1: "counted", 3: "material tag"})},
               source={"synthetic": True}, evidence=["synthetic sample"], confidence="proven", raw=bytes(21),
               dependencies=[{"kind": "model", "field": "model_0", "id": models["tower"], "status": "resolved"}], models=[models["tower"]]),
        marker(f"{p}:marker:hazard", p, "ga_kind_27", "hazard", [-300, 0, 0], 0,
               {"hazard_size": field(0, "u8", 1, "proven", "size", enum={0: "half-extent 5", 1: "half-extent 150"}, closed=True)},
               source={"synthetic": True}, evidence=["synthetic sample"], confidence="proven", raw=bytes(2)),
        marker(f"{p}:marker:raw", p, "ga_kind_26", "untraced_handler", [700, 0, 700], 0, {}, source={"synthetic": True},
               evidence=["synthetic unresolved record"], confidence="unresolved", raw=b"\x1a\x01\x02\x03"),
    ]
    instances = [{"id": f"{p}:placement:ground", "model": models["ground"], "translation": [0, 0, 0], "yaw": 0, "category": "model"}]
    shapes = []
    for index, (x, z, yaw) in enumerate(((-400, -200, 0), (400, 200, 0x2000), (0, -900, 0x4000))):
        pid = f"{p}:placement:building{index}"
        instances.append({"id": pid, "model": models["building"], "translation": [x, 0, z], "yaw": yaw, "category": "model"})
        vertical = ({"status": "none_stored", "min": None, "max": None, "reference": None, "evidence": "synthetic planar"} if planar
                    else {"status": "proven", "min": 0, "max": 240, "reference": "placement", "evidence": "synthetic"})
        shapes.append(collision(pid + ":collision", "footprint" if planar else "box", [x, 0, z], yaw, [-100, 0, -100], [100, 0, 100],
                                vertical=vertical, orientation="oriented", space="local", source={"synthetic": True},
                                evidence=["synthetic sample"], confidence="proven", flags=1))
    instances.append({"id": f"{p}:placement:tower", "model": models["tower"], "translation": [500, 0, -500], "yaw": 0, "category": "model"})
    navigation = {"points": [{"index": i, "x": float(x), "z": float(z)} for i, (x, z) in enumerate(((-800, 0), (0, 0), (800, 0), (0, 600)))],
                  "point_count": 4, "links": [{"index": 0, "targets": [1, 2, 3], "values": [10, 20, 30], "raw": [1, 10, 2, 20, 3, 30]}],
                  "link_fields": {}, "tables": [], "rom_range": [0, 0], "evidence": "synthetic"}
    texture_id = f"{p}:texture:minimap"
    return {
        "id": f"{p}:scene:sample", "schema": "btga-neutral", "schema_version": 1, "kind": "scene",
        "source": {"platform": p, "synthetic": True, "sample": name},
        "semantics_version": SCENE_SCHEMA_VERSION,
        "instances": instances, "collision": shapes, "markers": markers,
        "environment": [{"kind": 37, "clear_and_fog_colour": [60, 80, 110], "sky_colour_a": [120, 150, 200], "sky_colour_b": [40, 60, 90],
                         "light_bytes": [0] * 12, "evidence": "synthetic"}],
        "bounds": {"map": {"x": [-1500, 1500], "z": [-1500, 1500], "status": "proven", "evidence": "synthetic"}},
        "minimap": {"status": "proven", "origin": [-1500, -1500], "scale": 0.02, "rotation": 0,
                    "transform": "pixel = trunc((world - origin) * scale)",
                    "selections": [{"variant": "default", "status": "selected",
                                    "image": {"table_index": 0, "texture": texture_id, "width": 60, "height": 60, "rom_range": None}}]},
        "navigation": navigation, "music": {"status": "unsupported"}, "unresolved": [], "models": sorted(set(models.values())),
    }


def write_sample_catalog(root: Path) -> Path:
    exporter = ex.Exporter(Path(root))
    textures = {}
    for platform, colours in (("n64_btga", ((90, 140, 70, 255), (70, 110, 55, 255))), ("ps1", ((150, 130, 90, 255), (120, 100, 70, 255))),
                              ("n64_bt1", ((110, 110, 120, 255), (85, 85, 95, 255)))):
        ground_tex = f"{platform}:texture:ground"
        wall_tex = f"{platform}:texture:wall"
        textures[ground_tex] = {"id": ground_tex, **checker(*colours)}
        textures[wall_tex] = {"id": wall_tex, **checker((200, 190, 170, 255), (160, 70, 60, 255), 16, 8)}
        textures[f"{platform}:texture:minimap"] = {"id": f"{platform}:texture:minimap", **checker((30, 60, 30, 255), (60, 100, 60, 255), 60, 10)}
        models = {"ground": f"{platform}:model:ground", "building": f"{platform}:model:building", "tower": f"{platform}:model:tower"}
        records = {
            models["ground"]: model_record(models["ground"], platform, plane_mesh(1500, ground_tex)),
            models["building"]: model_record(models["building"], platform, box_mesh((-100, 0, -100), (100, 240, 100), wall_tex)),
            models["tower"]: model_record(models["tower"], platform, box_mesh((-40, 0, -40), (40, 600, 40), wall_tex, 3.0)),
        }
        name = {"n64_btga": "Sample N64 Global Assault", "ps1": "Sample PS1", "n64_bt1": "Sample BattleTanx"}[platform]
        scene = sample_scene(platform, name, models, planar=platform != "n64_btga")
        exporter.sources.append({"platform": platform, "title": name, "synthetic": True})
        exporter.add_scene(scene, records, textures, {"platform": platform, "level": "sample", "name": name, "campaign": "Synthetic samples",
                                                       "variant": "default", "variants": ["default"]})
    library_tank = "common:model:tank"
    exporter.models[library_tank] = model_record(library_tank, "n64_btga", box_mesh((-30, 0, -50), (30, 40, 50), None), "common_world")
    exporter.finish()
    return Path(root)


def write_sample_project(catalog: Path, path: Path) -> Path:
    """A small project over the N64-tagged sample map: one placed prop, one moved spawn, one field edit."""

    from tools.btga_assets.studio import StudioLibrary
    from tools.btga_studio.core import project
    from tools.btga_studio.core.document import EditorDocument
    from tools.btga_studio.core.scene import EditorScene

    library = StudioLibrary.open(Path(catalog))
    level = next(l.id for l in library.levels() if l.platform == "n64_btga")
    document = EditorDocument(EditorScene(library, level))
    document.place_model("n64_btga:model:tower", (-200.0, 0.0, 600.0), 0x2000)
    document.set_transform("n64_btga:marker:start_b", [800.0, 0.0, 900.0], 0xC000)
    document.set_field("n64_btga:marker:spawner", "spawn_budget", 25)
    project.save(Path(path), document, "Synthetic sample project")
    return Path(path)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("output", type=Path)
    parser.add_argument("--project", type=Path, help="also write a sample project file here")
    args = parser.parse_args(argv)
    write_sample_catalog(args.output)
    if args.project:
        write_sample_project(args.output, args.project)
    print(f"sample catalog written ({len(list(args.output.rglob('*')))} files)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
