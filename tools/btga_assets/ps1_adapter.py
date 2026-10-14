"""PS1 Global Assault adapter emitting the shared v2 scene/model/texture form.

Builds on the proven PS1 decoders (ps1_formats, ps1_model, scene): models
become shared meshes (mesh.ps1_mesh), textures carry IDs and wrap/alpha
metadata, and placements become instances with the same yaw convention
the N64 adapters use (the 0x800249A0 matrix equals the N64 one).
"""

from __future__ import annotations

from pathlib import Path

from . import ps1_formats as pf
from .mesh import bounds, ps1_mesh
from .neutral import PS1_COORDINATES
from .scene import LevelSet, assemble_scene, decode_level_models, model_textures, texture_table


def level_assets(corpus: Path, stem: str) -> tuple[dict, dict[str, dict], dict[str, dict]]:
    level = LevelSet(corpus, stem)
    scene, models, texture_maps = assemble_scene(level)
    table, _ = texture_table(level)
    textures: dict[str, dict] = {}
    for texture in table.values():
        textures[texture["key"]] = {
            "id": texture["key"],
            "width": texture["width"],
            "height": texture["height"],
            "format": texture["metadata"]["mode"],
            "wrap": {"s": "clamp", "t": "clamp"},
            "alpha": "mask" if texture["has_transparent_texels"] else "opaque",
            "png": texture["png"],
            "source": texture["metadata"],
        }
    records: dict[str, dict] = {}
    by_index: dict[int, str] = {}
    for index, record in models.items():
        slot_map = {slot: ({"id": t["key"], "width": t["width"], "height": t["height"], "alpha": "mask" if t["has_transparent_texels"] else "opaque"} if t else None)
                    for slot, t in texture_maps[index].items()}
        converted = dict(record)
        converted["mesh"] = ps1_mesh(record, slot_map)
        converted["bounds"] = bounds(converted["mesh"])
        records[record["id"]] = converted
        by_index[index] = record["id"]
    from . import ps1_semantics as sem
    from .semantics import SCENE_SCHEMA_VERSION

    bin_data = level.files[".BIN"][1]
    instances, markers, collision, lanes = [], [], [], []
    for placement in scene["placements"]:
        base = {"id": placement["id"], "translation": placement["translation"], "yaw": placement["word_6"],
                "extras": {"kind": placement["kind"], "status": placement["status"], "placed_by_loader": placement["placed_by_loader"]}}
        section = placement["section3"]
        raw = bin_data[section["offset"] : section["offset"] + section["size"]]
        if placement["model_index"] is not None:
            model_id = by_index[placement["model_index"]]
            instances.append({**base, "model": model_id, "category": "model"})
            shape = sem.collision_for(placement, records[model_id], model_id)
            if shape is not None:
                shape["extras"]["placed_by_loader"] = placement["placed_by_loader"]
                collision.append(shape)
        else:
            lane = sem.lane_bounds(placement, raw)
            if lane is not None:
                lanes.append(lane)
            typed = sem.typed_marker(placement, raw)
            if typed is not None:
                typed["source"]["placed_by_loader"] = placement["placed_by_loader"]
                markers.append(typed)
    floor = None
    if ".FLB" in level.files:
        flb_path, flb_data = level.files[".FLB"]
        floor = sem.floor_layer(pf.parse_flb(flb_data, flb_path), flb_path, pf.sha256(flb_data))
    out = dict(scene)
    out.update(
        {
            "semantics_version": SCENE_SCHEMA_VERSION,
            "coordinate_system": PS1_COORDINATES,
            "instances": instances,
            "collision": collision,
            "markers": markers,
            "environment": [],
            "floor": floor,
            "bounds": {"floor": scene["floor"], "lanes": lanes, "map": ({"x": floor["extent"]["x"], "z": floor["extent"]["z"], "status": "floor lattice extent",
                                                         "evidence": "lattice points span this range; no PS1 map boundary test is traced"} if floor else None)},
            "minimap": {"status": "paired_by_name", "selections": [],
                        "source": "RADAR/<level>.TEX (pairing by name; loader path assets\\radar\\ at 0x8003BDC8)",
                        "radar_file": level.stem.split("/")[0] + "/RADAR/" + level.stem.rsplit("/", 1)[-1] + ".TEX"},
            "navigation": None,
            "music": {"status": "unsupported"},
            "unresolved": [],
            "models": sorted(records),
        }
    )
    return out, records, textures


def library_assets(corpus: Path, stem: str) -> tuple[dict[str, dict], dict[str, dict]]:
    """Every model of a non-level bank (ENUMS) as shared meshes, with its textures."""

    level = LevelSet(corpus, stem)
    models = decode_level_models(level)
    table, problems = texture_table(level)
    textures = {
        t["key"]: {"id": t["key"], "width": t["width"], "height": t["height"], "format": t["metadata"]["mode"], "wrap": {"s": "clamp", "t": "clamp"},
                   "alpha": "mask" if t["has_transparent_texels"] else "opaque", "png": t["png"], "source": t["metadata"]}
        for t in table.values()
    }
    records = {}
    for index, record in models.items():
        slot_textures = model_textures(record, table, problems)
        slot_map = {slot: ({"id": t["key"], "width": t["width"], "height": t["height"], "alpha": "mask" if t["has_transparent_texels"] else "opaque"} if t else None)
                    for slot, t in slot_textures.items()}
        converted = dict(record)
        converted["mesh"] = ps1_mesh(record, slot_map)
        converted["bounds"] = bounds(converted["mesh"])
        records[record["id"]] = converted
    return records, textures
