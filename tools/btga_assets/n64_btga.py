"""BattleTanx: Global Assault (N64 USA Rev 0) adapter into the neutral contract.

Evidence (RAM addresses; matching sources under src/code):

* func_800E8380 selects each level's LZARI world bundles; func_800BA6C0
  loads them, giving model/part/reference indices bundle-relative bases.
* A world model's parts are LODs (model_draw_select_lod: lodMode 2 switches
  at 300 units, lodMode 3 at 1000 and 1700); each LOD lists pool
  references {geometry DL, state DL, texture DL} drawn by func_8007B1F0.
* Placement (x, y, z) is copied as pos = (x, z, y) with file y vertical;
  func_800DF0D0 / func_8009EFD4 build the object matrix
  [[c,0,-s],[0,1,0],[s,0,c]] (row-vector form) from the u16 yaw, 65536
  units per turn, i.e. a rotation about +Y.
* func_800DFA5C dispatches definitions by kind.  Kinds 0, 1, 2, 11, 14,
  22, 34, 35, 43, 44 draw the model at definition +2; kinds 1, 2, 11, 14,
  22, 35, 42, 43 create grid collision boxes from that model's bounds
  through func_800B1898; kind 30 creates explicit boxes or map lanes.
* Kind 37 sets the frame clear and fog colour (bytes 1-3, used by
  func_8007A0A0 as G_SETFILLCOLOR and G_SETFOGCOLOR), twelve light bytes
  for func_800A9C24 and two sky colours for func_800F7150; kind 38 sets a
  per-area colour for func_800A9BF0.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field

from . import n64_gbi as gbi
from . import n64_sources as src
from .mesh import MeshBuilder, MeshError, bounds
from .n64_binary import FormatError, sha256
from .neutral import SCHEMA, SCHEMA_VERSION, envelope

PLATFORM = "n64_btga"
COORDINATES = {
    "neutral_from_source": "identity (file x, y, z with y vertical)",
    "up_axis": "+Y",
    "handedness": "right",
    "front_face": "counter-clockwise (G_CULL_BACK)",
    "unit": "N64 world unit (placement s16)",
    "evidence": {
        "vertical_axis": "proven: func_800BA6C0 takes group height from y and passes placement y as pos[2]",
        "rotation": "proven: func_8009EFD4 matrix from the u16 yaw (65536 per turn)",
        "front_face": "GBI convention: G_CULL_BACK removes clockwise screen triangles",
    },
}
MODEL_KINDS = {0, 1, 2, 11, 14, 22, 34, 35, 43, 44}
COLLISION_FLAGS = {1: 1, 2: 0x20, 11: 0x40, 14: 0x4000, 22: 0x80, 42: 1}
LOD_SWITCH = {1: [], 2: [300.0], 3: [1000.0, 1700.0]}


def content_id(kind: str, rom_sha256: str, content) -> str:
    text = json.dumps(content, sort_keys=True, separators=(",", ":"))
    return f"{PLATFORM}:{kind}:{hashlib.sha256(f'{rom_sha256}|{kind}|{text}'.encode()).hexdigest()[:24]}"


@dataclass
class Bundle:
    index: int
    rom_range: tuple[int, int]
    data: bytes
    world: dict
    model_base: int


@dataclass
class Level:
    level: int
    name: str | None
    variant: str
    bundles: list[Bundle]
    problems: list[str] = field(default_factory=list)


def load_level(rom: bytes, slot_source: str, level: int, variant: str = "default") -> Level:
    from tools.btga_world import WorldError, parse_world

    catalog = {entry["level"]: entry for entry in src.ga_level_catalog(slot_source)}
    if level not in catalog:
        raise FormatError(f"Global Assault has no level {level}")
    ranges = catalog[level]["variants"][variant]
    bundles = []
    base = 0
    for index, (start, end) in enumerate(ranges):
        data = src.decode_stream(rom, start, end)
        try:
            world = parse_world(data)
        except WorldError as error:
            raise FormatError(f"level {level} bundle {index}: {error}") from error
        bundles.append(Bundle(index, (start, end), data, world, base))
        base += len(world["models"])
    return Level(level, catalog[level]["name"], variant, bundles)


# ------------------------------------------------------------- models -----


def _st_to_uv(st: int, scale: int, shift: int, origin: float, size: int) -> float:
    texel = st / 32.0 * (scale / 65536.0 if scale != 0xFFFF else 1.0)
    if 1 <= shift <= 10:
        texel /= 1 << shift
    elif shift >= 11:
        texel *= 1 << (16 - shift)
    return (texel - origin) / size


def _wrap(mode: int) -> str:
    if mode & 2:
        return "clamp"
    return "mirror" if mode & 1 else "repeat"


def chunk(rom: bytes, pool: str, offset: int, size: int) -> bytes:
    start, end = src.GA_POOLS[pool]
    if offset < 0 or size <= 0 or start + offset + size > end:
        raise FormatError(f"{pool} reference {offset:#x}+{size:#x} leaves its pool")
    return rom[start + offset : start + offset + size]


def decode_reference(rom: bytes, rom_sha: str, reference: dict, textures: dict[str, dict]) -> dict:
    """Decode one {geometry, state, texture} pool reference."""

    name = f"ref g{reference['geometry_offset']:#x}"
    state = gbi.RenderState()
    texture_payload = b""
    texture_commands = []
    if reference["texture_offset"] >= 0:
        texture_payload = chunk(rom, "texture", reference["texture_offset"], reference["texture_size"])
        start, texture_commands = gbi.find_stream(texture_payload, gbi.F3DEX2, {0xE3, 0xE6, 0xE7, 0xE8, 0xF0, 0xF3, 0xF5, 0xFD})
        gbi.run_state(texture_payload, texture_commands, gbi.F3DEX2, state, name)
    state_payload = chunk(rom, "state", reference["state_offset"], reference["state_size"])
    _, state_commands = gbi.find_stream(state_payload, gbi.F3DEX2, {0xD7, 0xD9, 0xE2, 0xE3, 0xE7, 0xF2, 0xF5, 0xFA, 0xFB, 0xFC})
    gbi.run_state(state_payload, state_commands, gbi.F3DEX2, state, name)
    geometry_payload = chunk(rom, "geometry", reference["geometry_offset"], reference["geometry_size"])
    _, geometry_commands = gbi.find_stream(geometry_payload, gbi.F3DEX2, {0x01, 0x05, 0x06})
    mesh = gbi.run_geometry(geometry_payload, geometry_commands, gbi.F3DEX2, name)

    texture_id = None
    texture = None
    if reference["texture_offset"] >= 0:
        key = [reference["texture_offset"], reference["texture_size"], reference["state_offset"], reference["state_size"]]
        texture_id = content_id("texture", rom_sha, key)
        if texture_id not in textures:
            described = gbi.texture_from_state(state, texture_payload, name)
            if described is not None and "rgba" in described:
                rgba = described["rgba"]
                alpha_values = {rgba[i] for i in range(3, len(rgba), 4)}
                textures[texture_id] = {
                    "id": texture_id,
                    "width": described["width"],
                    "height": described["height"],
                    "format": described["format"],
                    "wrap": {"s": _wrap(described["wrap"]["s"]), "t": _wrap(described["wrap"]["t"])},
                    "alpha": "opaque" if alpha_values == {255} else ("mask" if alpha_values <= {0, 255} else "blend"),
                    "rgba": rgba,
                    "source": {"platform": PLATFORM, "texture_pool": key[:2], "state_pool": key[2:], "image_address": described["image_address"],
                               "palette": described["palette"], "tile_origin": described["tile_origin"], "wrap_raw": described["wrap"]},
                }
            else:
                textures[texture_id] = {"id": texture_id, "status": (described or {}).get("status", "no texture load"), "source": {"texture_pool": key[:2]}}
        texture = textures[texture_id] if "rgba" in textures[texture_id] else None
    return {"state": state, "mesh": mesh, "texture_id": texture_id if texture else None, "texture": texture,
            "unknown_commands": state.unknown, "geometry_payload_overlap": mesh.overlaps_commands}


def model_record(rom: bytes, rom_sha: str, level: Level, bundle_index: int, model_index: int, textures: dict[str, dict]) -> dict:
    bundle = level.bundles[bundle_index]
    world = bundle.world
    if not 0 <= model_index < len(world["models"]):
        raise FormatError(f"model {model_index} outside bundle {bundle_index}")
    model = world["models"][model_index]
    lods = []
    for part_index in range(model["first_part"], model["first_part"] + model["part_count"]):
        part = world["parts"][part_index]
        refs = world["references"][part["first_reference"] : part["first_reference"] + part["reference_count"]]
        lods.append([{k: ref[k] for k in sorted(ref)} for ref in refs])
    record_id = content_id("model", rom_sha, lods)
    builder = MeshBuilder()
    switch = LOD_SWITCH.get(model["part_count"], [])
    reference_records = []
    for lod_index, refs in enumerate(lods):
        builder.begin_lod(lod_index, switch[lod_index] if lod_index < len(switch) else None)
        for ref in refs:
            decoded = decode_reference(rom, rom_sha, ref, textures)
            state, mesh, texture = decoded["state"], decoded["mesh"], decoded["texture"]
            mode = state.geometry_mode
            cull_back, cull_front = bool(mode & gbi.F3DEX2.cull_back), bool(mode & gbi.F3DEX2.cull_front)
            lit = bool(mode & gbi.F3DEX2.lighting)
            material = builder.material(
                {
                    "id": f"{decoded['texture_id'] or 'untextured'}|cull{int(cull_front)}{int(cull_back)}|lit{int(lit)}",
                    "texture": decoded["texture_id"],
                    "double_sided": not cull_back and not cull_front,
                    "alpha_mode": "MASK" if texture and texture["alpha"] == "mask" else ("BLEND" if texture and texture["alpha"] == "blend" else "OPAQUE"),
                    "geometry_mode": f"{mode:08x}",
                    "othermode_h": f"{state.othermode_h:08x}",
                    "othermode_l": f"{state.othermode_l:08x}",
                    "combine": [f"{state.combine[0]:06x}", f"{state.combine[1]:08x}"],
                    "prim_colour": state.prim_colour,
                    "env_colour": state.env_colour,
                    "vertex_colour": "normal (lit)" if lit else "shade RGBA, 255 = 1.0",
                }
            )
            base = len(builder.positions)
            for vertex in mesh.vertices:
                builder.position(vertex["position"])
            tile = state.tiles[state.texture_tile]
            for t_index, triangle in enumerate(mesh.triangles):
                order = triangle if not (cull_front and not cull_back) else (triangle[0], triangle[2], triangle[1])
                corners = []
                for v in order:
                    vertex = mesh.vertices[v]
                    a, b, c, d = vertex["rgba"]
                    corner = {"p": base + v, "uv": None, "colour": None, "normal": None}
                    if lit:
                        normal = [x - 256 if x > 127 else x for x in (a, b, c)]
                        length = sum(x * x for x in normal) ** 0.5
                        corner["normal"] = [x / length for x in normal] if length else None
                    else:
                        corner["colour"] = [a / 255, b / 255, c / 255, d / 255]
                    if texture:
                        s, t = vertex["st"]
                        corner["uv"] = [
                            _st_to_uv(s, state.texture_scale[0], tile.shifts, tile.uls / 4.0, texture["width"]),
                            _st_to_uv(t, state.texture_scale[1], tile.shiftt, tile.ult / 4.0, texture["height"]),
                        ]
                    corners.append(corner)
                source = {"geometry_offset": ref["geometry_offset"], "triangle": t_index}
                try:
                    builder.triangle(material, corners, source)
                except MeshError as error:
                    builder.reject(source, str(error))
            reference_records.append(
                {
                    "lod": lod_index,
                    **ref,
                    "vertex_count": len(mesh.vertices),
                    "triangle_count": len(mesh.triangles),
                    "texture": decoded["texture_id"],
                    "unknown_commands": decoded["unknown_commands"],
                    "vertex_flags_nonzero": sum(1 for v in mesh.vertices if v["flag"]),
                    "vertex_command_overlap": decoded["geometry_payload_overlap"],
                }
            )
        builder.end_lod()
    built = builder.build()
    record = envelope(
        "model",
        record_id,
        {"platform": PLATFORM, "rom_sha256": rom_sha, "level": level.level, "variant": level.variant, "bundle": bundle_index,
         "bundle_rom_range": list(bundle.rom_range), "model_index": model_index},
    )
    record.update(
        {
            "coordinate_system": COORDINATES,
            "bounds_record": {k: model[k] for k in ("min_x", "min_y", "min_z", "max_x", "max_y", "max_z")},
            "lod_mode": model["part_count"],
            "references": reference_records,
            "mesh": built,
            "bounds": bounds(built),
        }
    )
    return record


# -------------------------------------------------------------- scene -----


def _definition(world: dict, offset: int) -> dict:
    for definition in world["definitions"]:
        if definition["offset"] == offset:
            return definition
    raise FormatError(f"no definition at {offset:#x}")


def scene_record(rom: bytes, rom_sha: str, level: Level, textures: dict[str, dict], images: dict[int, dict] | None = None,
                 conditions: list[str] | None = None) -> tuple[dict, dict[str, dict]]:
    """Neutral scene for one level stream set.

    ``conditions`` lists every variant label that loads exactly these
    streams (the minimap depends on the loading condition, not the streams);
    ``images`` maps image-table indices to their ROM ranges.
    """

    from . import n64_btga_semantics as sem
    from .semantics import SCENE_SCHEMA_VERSION
    from .semantics import marker as marker_record

    models: dict[str, dict] = {}
    cache: dict[tuple[int, int], str] = {}
    instances, collision, markers, environment, unresolved = [], [], [], [], []
    lanes = []

    def model_id(bundle_index: int, index: int, kind: int | None = None) -> str | None:
        if index == 0xFFFF:
            return None
        key = (bundle_index, index)
        if key not in cache:
            if index >= len(level.bundles[bundle_index].world["models"]):
                unresolved.append({"bundle": bundle_index, "model_index": index, "kind": kind,
                                   "reason": "a proven model lookup (func_800DF758 / LOOK) indexes past this bundle's models"})
                return None
            record = model_record(rom, rom_sha, level, bundle_index, index, textures)
            models[record["id"]] = record
            cache[key] = record["id"]
        return cache[key]

    def place(bundle: Bundle, definition: dict, placement: dict, group: int, number: int, condition: list) -> None:
        kind = definition["kind"]
        payload = bytes.fromhex(definition["raw_hex"])
        fields = definition.get("fields", {})
        pid = f"{PLATFORM}:placement:{level.level}:{level.variant}:{bundle.index}:{number}"
        translation = [placement["x"], placement["y"], placement["z"]]
        yaw = placement["yaw"]
        source = {"bundle": bundle.index, "definition_offset": definition["offset"], "placement_index": number, "group": group, "kind": kind}
        base = {"id": pid, "translation": translation, "yaw": yaw,
                "extras": {"kind": kind, "group": group, "definition_offset": definition["offset"], "conditions": condition}}
        if kind == 39:
            target = _definition(bundle.world, definition["target_offset"])
            place(bundle, target, placement, group, number, condition + [{"condition": definition["condition"], "flag": definition["flag"]}])
            return
        if kind in MODEL_KINDS:
            mid = model_id(bundle.index, fields["model_index"], kind)
            if mid:
                instances.append({**base, "model": mid, "category": "model"})
        if kind in (1, 2, 11, 14, 22, 35, 42, 43) and fields.get("model_index", 0xFFFF) != 0xFFFF:
            index = fields["model_index"]
            if index < len(bundle.world["models"]):
                bounds_ = bundle.world["models"][index]
                flags = COLLISION_FLAGS.get(kind, 1)
                if kind == 35 and payload[6]:
                    flags = 0x20
                if kind == 43 and payload[5]:
                    flags = 0x4000
                if kind != 2 or bounds_["max_y"] >= 2:
                    shape = sem.model_box(pid, translation, yaw, bounds_, flags, kind, 5000 - placement["y"] if kind == 43 else None,
                                          {**source, "model": model_id(bundle.index, index, kind)})
                    shape["extras"].update({"conditions": condition, "collision_only": kind == 42})
                    collision.append(shape)
        if kind == 30:
            values = [int.from_bytes(payload[2 * i : 2 * i + 2], "big", signed=True) for i in range(7)]
            mode = payload[1]
            if mode == 0:
                lanes.append({"group": group, "x": [placement["x"] + values[1], placement["x"] + values[4]],
                              "z": [placement["z"] + values[3], placement["z"] + values[6]],
                              "source": source, "evidence": "func_800DFA5C case 30 mode 0 writes world->lanes[group] = placement + p[1]/p[4], p[3]/p[6]"})
            elif mode in (1, 2):
                shape = sem.explicit_box(pid, translation, yaw, values, 0x01000000 if mode == 1 else 0x80000, source)
                shape["extras"]["conditions"] = condition
                collision.append(shape)
            else:
                from .semantics import field as field_spec

                box = {name: field_spec(values[i], "s16", 2 * i, "proven", f"trigger box {name} relative to the placement (func_800E9990 argument)")
                       for i, name in ((1, "x0"), (2, "y0"), (3, "z0"), (4, "x1"), (5, "y1"), (6, "z1"))}
                box["trigger_mode"] = field_spec(mode, "u8", 1, "proven", "3 or 4; func_800E9990's last argument is (mode == 4)",
                                                 enum={3: "mode 3", 4: "mode 4"}, closed=True)
                markers.append(marker_record(pid + ":marker", PLATFORM, "ga_kind_30", "mission_trigger", translation, yaw, box,
                                             source=source, evidence=["func_800DFA5C case 30 modes 3/4 -> func_800E9990"], confidence="proven",
                                             raw=payload, conditions=condition))
        if kind == 37:
            environment.append({"kind": 37, "clear_and_fog_colour": list(payload[1:4]), "light_bytes": list(payload[4:16]),
                                "sky_colour_a": list(payload[19:22]), "sky_colour_b": list(payload[16:19]),
                                "evidence": "func_800B05F4 -> func_800B04E0: fog/clear via func_80079C00, lights via func_800A9C24, sky via func_800F7150"})
        if kind == 38:
            environment.append({"kind": 38, "group": group, "area_colour": list(payload[1:4]), "evidence": "func_800B06A8 -> func_800A9BF0"})
        typed = sem.typed_marker(level, bundle, definition, placement, group, number, condition, model_id)
        if typed is not None:
            markers.append(typed)
            shape = sem.handler_box(pid, translation, yaw, kind, payload, source)
            if shape is not None:
                shape["extras"]["conditions"] = condition
                collision.append(shape)
        elif kind in (9, 25, 33):
            markers.append(marker_record(pid + ":marker", PLATFORM, f"ga_kind_{kind}", "ignored_by_dispatcher", translation, yaw, {},
                                         source=source, evidence=["func_800DFA5C has no case for this kind"], confidence="proven",
                                         raw=payload, conditions=condition))

    for bundle in level.bundles:
        world = bundle.world
        for group_index, group in enumerate(world["groups"]):
            for index in range(group["first_placement"], group["first_placement"] + group["placement_count"]):
                placement = world["placements"][index]
                place(bundle, _definition(world, placement["definition_offset"]), placement, group_index, index, [])

    groups = [
        {"bundle": b.index, "group": i, **{k: g[k] for k in ("min_x", "min_y", "min_z", "max_x", "max_y", "max_z")}}
        for b in level.bundles for i, g in enumerate(b.world["groups"])
    ]
    x0, z0, x1, z1 = sem.area0_box(level.bundles)
    scene = envelope(
        "scene",
        f"{PLATFORM}:scene:{level.level}:{level.variant}:{hashlib.sha256((rom_sha + ''.join(sha256(b.data) for b in level.bundles)).encode()).hexdigest()[:16]}",
        {"platform": PLATFORM, "rom_sha256": rom_sha, "level": level.level, "name": level.name, "variant": level.variant,
         "bundles": [{"index": b.index, "rom_range": list(b.rom_range), "decoded_sha256": sha256(b.data)} for b in level.bundles]},
    )
    scene.update(
        {
            "semantics_version": SCENE_SCHEMA_VERSION,
            "coordinate_system": COORDINATES,
            "instances": instances,
            "collision": collision,
            "markers": markers,
            "environment": environment,
            "bounds": {"groups": groups, "lanes": lanes,
                       "map": {"x": [x0, x1], "z": [z0, z1], "status": "proven",
                               "evidence": "func_800BA6C0 forms this group-0 box; func_800B07D4 builds the group-0 collision grid and the minimap transform over it"}},
            "minimap": sem.minimap_record(level, images or {}, rom_sha, conditions or [level.variant]),
            "navigation": None,
            "music": {"status": "unsupported: no level-to-song selection traced"},
            "unresolved": unresolved,
            "models": sorted(models),
        }
    )
    return scene, models
