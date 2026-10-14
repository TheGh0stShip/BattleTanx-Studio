"""Global Assault (N64) world semantics: minimap, collision and typed markers.

Every rule below cites the matching decompilation under src/code.

Minimap (func_800B07D4, src/code/unknown/800B06E0.c)
    func_800BA6C0 builds the world, then calls func_800B06E0, which builds
    one collision grid per world group.  Grid 0 doubles as the minimap
    descriptor read by the HUD (800C7C10_race_map_draw.c reads
    D_803977F0 = grid[0].x0/z0, D_80397800 = grid[0].scale and
    D_80397804 = grid[0].tbl):
      * x0, z0, w = x1 - x0, h = z1 - z0 come from world group 0's box,
        which func_800BA6C0 forms as the union of every bundle's group-0
        header box, replaced by +/-9000 when it is degenerate;
      * scale = min(70.0f / w, 70.0f / h);
      * tbl (the image) is chosen by switch (D_8021949C = level) under
        func_8009D144() (game mode D_802194A0 in 7..14), the same predicate
        func_800AF43C passes to func_800E8380 as the stream-variant flag;
      * case 25 stores 0; levels without a case leave tbl unchanged (the
        previous level's pointer).
    The descriptors D_801179FC..D_80117DB4 are entries 161..195 of the
    28-byte image table at 0x80116860 (ROM 0xA6860).
    func_800C7C10 plots a world point (x, z) at
    trunc((x - x0) * scale), trunc((z - z0) * scale) and draws it only when
    it lies inside the image's width and height; there is no rotation.
"""

from __future__ import annotations

import struct

from . import n64_sources as src
from .semantics import collision as collision_record
from .semantics import field, marker

PLATFORM = "n64_btga"
IMAGE_TABLE_RAM = 0x80116860
MINIMAP_SPAN = 70.0
# func_800B07D4: {func_8009D144() result: {level: image-table index or None}}
MINIMAP_SELECTION = {
    True: {0: 185, 24: 185, 1: 194, 2: 186, 3: 175, 4: 173, 5: 195, 6: 180, 7: 193, 8: 191, 9: 167, 10: 171, 11: 176,
           12: 165, 13: 169, 14: 178, 15: 183, 16: 161, 25: None, 26: 182},
    False: {1: 194, 2: 187, 3: 175, 4: 174, 5: 195, 6: 181, 7: 193, 8: 192, 9: 168, 10: 172, 11: 177, 12: 166, 13: 170,
            14: 179, 15: 184, 16: 161, 17: 162, 18: 163, 19: 164, 20: 188, 26: 188, 21: 189, 22: 190, 23: 190, 0: 185,
            24: 185, 25: None},
}
MINIMAP_EVIDENCE = [
    "func_800B07D4 (800B06E0.c): grid 0 origin, scale = min(70/w, 70/h), image switch on D_8021949C under func_8009D144()",
    "func_800C7C10 (800C7C10_race_map_draw.c): pixel = trunc((world - origin) * scale), drawn inside image w/h",
    "func_800BA6C0 (800BA6C0_world_loader.c): group-0 box = union of bundle group-0 headers, +/-9000 when degenerate",
    "func_800AF43C: the same func_8009D144() result selects the func_800E8380 stream variant",
]
VARIANT_PREDICATE = {"default": False, "flag": True, "mode10": False}


def _f32(value: float) -> float:
    return struct.unpack(">f", struct.pack(">f", value))[0]


def area0_box(bundles: list) -> tuple[int, int, int, int]:
    """World group 0's box exactly as func_800BA6C0 builds it (x0, z0, x1, z1)."""

    first = bundles[0].world["groups"]
    g = first[0] if first else {"min_x": 0, "min_z": 0, "max_x": 0, "max_z": 0}
    x0, z0, x1, z1 = g["min_x"], g["min_z"], g["max_x"], g["max_z"]
    for bundle in bundles[1:]:
        if bundle.world["groups"]:
            other = bundle.world["groups"][0]
            x0, z0 = min(x0, other["min_x"]), min(z0, other["min_z"])
            x1, z1 = max(x1, other["max_x"]), max(z1, other["max_z"])
    if x1 == x0:
        x0, z0, x1, z1 = -9000, -9000, 9000, 9000
    return x0, z0, x1, z1


def minimap_record(level, images: dict[int, dict], rom_sha: str, conditions: list[str]) -> dict:
    """The minimap each loading condition of this stream set selects."""

    from .n64_btga import content_id

    x0, z0, x1, z1 = area0_box(level.bundles)
    w, h = (x1 - x0) & 0xFFFF, (z1 - z0) & 0xFFFF
    scale = None
    if w and h:
        scale = _f32(MINIMAP_SPAN / w)
        other = _f32(MINIMAP_SPAN / h)
        scale = other if other < scale else scale
    selections = []
    for condition in sorted(set(conditions)):
        predicate = VARIANT_PREDICATE[condition]
        table = MINIMAP_SELECTION[predicate]
        entry = {"variant": condition, "func_8009D144": predicate}
        if level.level not in table:
            entry.update({"status": "unchanged", "image": None,
                          "note": "no case for this level: the grid keeps the previous level's image pointer"})
        elif table[level.level] is None:
            entry.update({"status": "none", "image": None, "note": "case stores 0: no minimap image"})
        else:
            index = table[level.level]
            image = images.get(index)
            entry.update({
                "status": "selected",
                "image": {
                    "table_index": index,
                    "descriptor_ram": f"0x{IMAGE_TABLE_RAM + 28 * index:08X}",
                    "texture": content_id("texture", rom_sha, ["image_table", index]),
                    "rom_range": [image["start"], image["end"]] if image else None,
                    "width": image["width"] if image else None,
                    "height": image["height"] if image else None,
                    "format": {"fmt": image["format"], "siz": image["size"], "name": "CI4" if (image["format"], image["size"]) == (2, 0) else None} if image else None,
                },
            })
            if image and scale:
                entry["fit"] = {"world_span_pixels": [round(w * scale, 3), round(h * scale, 3)],
                                "image_pixels": [image["width"], image["height"]]}
        selections.append(entry)
    return {
        "status": "proven",
        "origin": [x0, z0],
        "area0_box": {"x0": x0, "z0": z0, "x1": x1, "z1": z1},
        "scale": scale,
        "rotation": 0,
        "transform": "pixel_x = trunc((world_x - origin_x) * scale); pixel_y = trunc((world_z - origin_z) * scale); "
                     "drawn only when 0 <= pixel < image size; no rotation or flip",
        "draw": {"prim_alpha": 0x96, "frame_drawn_when": "D_80117EB0 < 3"},
        "selections": selections,
        "evidence": MINIMAP_EVIDENCE,
    }


# --------------------------------------------------------------- markers --

GAME_MODE = "D_802194A0"
MODEL_LOOKUP = "func_800DF758 (model table, bundle model_base + index)"


def _u(payload: bytes, offset: int, kind: str):
    if offset + {"u8": 1, "u16": 2, "s16": 2, "u32": 4, "s32": 4}[kind] > len(payload):
        return None
    code = {"u8": ">B", "u16": ">H", "s16": ">h", "u32": ">I", "s32": ">i"}[kind]
    return struct.unpack_from(code, payload, offset)[0]


def _model_field(payload: bytes, offset: int, meaning: str) -> dict | None:
    value = _u(payload, offset, "u16")
    if value is None:
        return None
    return field(value, "u16", offset, "proven", meaning, reference="model", sentinel={0xFFFF: "none"})


# kind -> (category, handler, [(name, offset, type, evidence, meaning, options)], [model field offsets], restrictions, confidence)
SCHEMAS = {
    13: ("unit_spawner", "func_800DE930 / func_800DEB1C (800DE930_unit_spawner_create.c, 800DEAC0_unit_spawner_update.c)", [
        ("spawn_mode", 1, "u8", "proven", "o->b40: 2/3 spawn every check (others 1 in 16); 3 sets unit flag 0x40; 4 requires player flag bit 2", {}),
        ("unit_parameter", 2, "u8", "proven", "o->b41: copied into every spawned unit (l.i56); meaning not traced further", {}),
        ("owner_slot", 3, "u8", "proven", "o->b23 = D_80219582[value]: player slot that owns the units (127 = none)", {}),
        ("spawn_budget", 4, "u16", "proven", "o->h24: units left to spawn; 0 disables the spawner, 0xFFFF never decrements; linked spawners share it",
         {"sentinel": {0: "disabled", 0xFFFF: "unlimited"}}),
        ("unit_type_mask", 6, "u16", "proven", "o->h28: unit types 0..14 the spawner cycles through (func_8009D914() % 15 must hit a set bit)",
         {"bits": 15, "legal": (1, 0x7FFF)}),
        ("spawn_interval", 8, "u32", "proven", "o->i36: ticks of D_8021945C between spawns", {}),
        ("max_spawned", 14, "u8", "proven", "o->b42: spawning stops after this many units (0 means 255)", {"sentinel": {0: "255"}}),
    ], [12], {}, "proven"),
    16: ("pickup", "func_800E9B50 (800E9B50_model_pickup_create.c)", [
        ("half_width_x", 1, "u8", "proven", "grid box x extent -value..+value (func_800B1898 flag 0x10000)", {}),
        ("half_width_z", 2, "u8", "proven", "grid box z extent -value..+value", {}),
        ("pickup_type", 3, "u8", "proven", "o->a; mode 14 registers type 0 with func_800C98D8", {}),
    ], [], {"modes_excluded": [0, 1, 2, 3, 6, 8, 11, 12, 13], "mode_variable": GAME_MODE}, "proven"),
    21: ("objective", "func_800E9E80 (800E9E80_mission_props_spawn.c)", [
        ("draw_band", 12, "u8", "proven", "desc->b12 passed to func_800DF63C with the model and height", {}),
        ("spawner_offset", 16, "s32", "proven", "desc->w16: byte offset of an embedded kind-13 spawner record in this bundle, or -1",
         {"sentinel": {-1: "none"}}),
        ("objective_class", 20, "u8", "proven", "desc->b20: 1 and 2 increment objective counter D_802194B0[0]; 3 records a material via func_800B9C68",
         {"enum": {0: "not counted", 1: "counted", 2: "counted", 3: "material tag"}}),
    ], [2, 4, 6, 8, 10], {}, "proven"),
    27: ("hazard", "func_800F1B30 (800F1B30_hazard_actor_spawn.c)", [
        ("hazard_size", 1, "u8", "proven", "nonzero selects size 1: grid half-extent D_80125AA0[size] = 5 or 150, height 0..10 (flag 256)",
         {"enum": {0: "half-extent 5", 1: "half-extent 150"}}),
    ], [], {}, "proven"),
    28: ("powerup_pad", "func_800F2BC0 (800F2BC0_powerup_pad_create.c)", [], [2, 4, 6], {}, "proven"),
    31: ("multiplayer_structure", "func_800E80F4 (800E80F4_multiplayer_actor_spawn.c)", [
        ("team_slot", 1, "u8", "proven", "D_80219582[value] must be below the player count D_802194A6", {}),
        ("mode11_model", 2, "u16", "proven", "model looked up by func_800DF758 only in game mode 11", {"reference": "model", "sentinel": {0xFFFF: "none"}}),
        ("structure_offset", 4, "s32", "proven", "byte offset of the structure definition (kind-3 layout) passed to func_800DD3A0", {}),
    ], [], {"modes_only": [1, 3, 11], "mode_variable": GAME_MODE}, "proven"),
    32: ("effect_prop", "func_800F5298 (800F5298_effect_prop_spawn.c)", [
        ("owner_slot", 1, "u8", "proven", "o->b27 = D_80219582[value]; counted in D_803AD950", {}),
        ("rate_parameter", 6, "u8", "proven", "byte read as a float into o->f60; the u16 at +6 is not a model", {}),
    ], [2, 4, 8, 10, 12, 14], {"modes_only": [4, 10], "mode_variable": GAME_MODE, "or": "D_80117EB4 == 10"}, "proven"),
    12: ("turret", "func_800E2AEC via func_800DFA5C case 12", [
        ("turret_mode", 1, "u8", "proven", "first argument of func_800E2AEC", {}),
        ("team_slot", 2, "u8", "proven", "D_80219582[value]; created only when below the player count D_802194A6", {}),
    ], [4], {}, "proven"),
    7: ("turret_random", "func_800D8C60 via func_800DFA5C case 7", [
        ("turret_mode", 1, "u8", "proven", "third argument of func_800D8C60", {}),
        ("team_slot", 2, "u8", "proven", "last argument of func_800D8C60", {}),
    ], [], {}, "proven"),
    17: ("effect_endpoint", "inline in func_800DFA5C case 17", [
        ("endpoint_index", 1, "u8", "proven", "index into world->e18[]", {}),
        ("endpoint_value", 2, "u16", "proven", "stored as world->e18[index].f", {}),
    ], [], {}, "proven"),
    45: ("effect_node", "func_800D7DE0 via func_800DFA5C case 45", [
        ("enable_bit", 1, "u8", "proven", "in modes 7..14 spawned only when D_8021958C->u58 has bit (1 << value)", {}),
        ("owner_slot", 2, "u8", "proven", "D_80219582[value]; 0x7F means no owner", {}),
    ], [], {}, "proven"),
    3: ("structure", "func_800DD3A0 (800DD3A0_structure_create.c)", [
        ("draw_band", 18, "u8", "proven", "d->b18 passed to func_800DF63C", {}),
        ("byte_19", 19, "u8", "unresolved", "d->b19 read by func_800DD3A0; role untraced", {}),
    ], [2, 4, 6, 8, 10, 12, 14, 16], {}, "proven"),
    4: ("turret_emplacement", "func_800E2308 (800E21A0_turret_draw_create.c)", [
        ("material_tag", 12, "u8", "proven", "nonzero records a material via func_800B9C68", {}),
    ], [2, 4, 6, 8, 10], {}, "proven"),
    5: ("foliage", "func_800E1540 (800E1540_foliage_prop_create.c)", [], [2, 4, 6], {}, "proven"),
    10: ("destructible", "func_800ED320 (800ED320_destructible_prop_create.c)", [
        ("collision_variant", 8, "u8", "proven", "nonzero selects grid flag 0x100002 instead of 0x100000", {}),
        ("respawning", 9, "u8", "proven", "nonzero creates object class 25 instead of 24", {}),
    ], [2, 4, 6], {}, "proven"),
    15: ("building", "func_800EAC30 (800EAC30_building_create.c)", [
        ("building_type", 8, "u8", "proven", "d->b8: 4 enlarges the object; 2/3/6 use grid flag 2, else 0x8000; 1/6 set o->b45", {}),
        ("building_parameter", 9, "u8", "proven", "d->b9 stored in o->b46", {}),
    ], [2, 4, 6, 10], {}, "proven"),
    20: ("crate", "func_800E6E5C (800E6B04_crate_unit.c)", [], [2], {"modes_only": [3, 7, 13, 14], "mode_variable": GAME_MODE}, "proven"),
    24: ("selection", "func_800ED990 (800ED990_entity_selection_create.c)", [], [2, 4, 6], {}, "proven"),
    29: ("barrel", "func_800F33C0 (800F32EC_barrel_handlers.c)", [], [2], {}, "proven"),
    36: ("flicker_prop", "func_800F7C30 (800F7C30_flicker_prop_create.c)", [], [2, 4], {}, "proven"),
    40: ("static_prop", "func_800F7EC0 (800F7EC0_static_prop_create.c)", [], [2], {}, "proven"),
}
# Bytes a handler reads; the world parser may extend a definition up to the
# next placement-referenced one, absorbing records reached only by offset.
RECORD_SIZES = {31: 8, 13: 15, 21: 21, 16: 4, 27: 2}
DEFINITION_SIZES = {3: 20, 13: 15}


def embedded_definition(bundle, offset: int, expected_kind: int) -> dict:
    """Resolve a definition reached by byte offset into the bundle's definition section."""

    base = bundle.world["offsets"][3]
    end = bundle.world["offsets"][4]
    record = {"kind": "definition", "bundle": bundle.index, "definition_offset": offset, "expected_kind": expected_kind}
    if not 0 <= offset < end - base:
        return {**record, "status": "outside the definition section"}
    found = bundle.data[base + offset]
    size = DEFINITION_SIZES.get(found, 1)
    raw = bundle.data[base + offset : min(end, base + offset + size)]
    return {**record, "found_kind": found, "raw_hex": raw.hex(),
            "status": "resolved" if found == expected_kind else f"kind {found} found, {expected_kind} expected"}


# Handlers whose descriptor reads are not yet traced: their fields stay raw.
UNTRACED = {6: "func_800B03F4", 8: "func_800E56D0 (bridge)", 26: "func_800EF770 (wave surface)", 41: "func_800F85F0 (artillery; payload unused)",
            23: "__dummy"}


def typed_marker(level, bundle, definition: dict, placement: dict, group: int, number: int, conditions: list,
                 model_id) -> dict | None:
    kind = definition["kind"]
    payload = bytes.fromhex(definition["raw_hex"])
    pid = f"{PLATFORM}:placement:{level.level}:{level.variant}:{bundle.index}:{number}"
    source = {"bundle": bundle.index, "bundle_rom_range": list(bundle.rom_range), "definition_offset": definition["offset"],
              "definition_size": len(payload), "placement_index": number, "group": group, "kind": kind}
    translation = [placement["x"], placement["y"], placement["z"]]
    if kind not in SCHEMAS:
        if kind not in UNTRACED:
            return None
        return marker(pid + ":marker", PLATFORM, f"ga_kind_{kind}", "untraced_handler", translation, placement["yaw"], {},
                      source=source, evidence=[f"func_800DFA5C case {kind} -> {UNTRACED[kind]}; descriptor reads untraced"],
                      confidence="unresolved", raw=payload, conditions=conditions)
    category, handler, specs, model_offsets, restrictions, confidence = SCHEMAS[kind]
    fields = {}
    for name, offset, kind_, evidence, meaning, options in specs:
        value = _u(payload, offset, kind_)
        if value is None:
            continue
        fields[name] = field(value, kind_, offset, evidence, meaning, legal=options.get("legal"), enum=options.get("enum"),
                             bits=options.get("bits"), reference=options.get("reference"), sentinel=options.get("sentinel"))
    dependencies, models = [], []
    for index, offset in enumerate(model_offsets):
        spec = _model_field(payload, offset, f"model looked up by {MODEL_LOOKUP}")
        if spec is None:
            continue
        fields[f"model_{index}"] = spec
        if spec["value"] != 0xFFFF:
            mid = model_id(bundle.index, spec["value"], kind)
            dependencies.append({"kind": "model", "field": f"model_{index}", "id": mid,
                                 "status": "resolved" if mid else "index outside this bundle's models"})
            if mid:
                models.append(mid)
    for name in ("mode11_model",):
        if name in fields and fields[name]["value"] != 0xFFFF:
            mid = model_id(bundle.index, fields[name]["value"], kind)
            dependencies.append({"kind": "model", "field": name, "id": mid, "status": "resolved" if mid else "index outside this bundle's models"})
    for name, expected in (("spawner_offset", 13), ("structure_offset", 3)):
        if name in fields and fields[name]["value"] != -1:
            dependency = embedded_definition(bundle, fields[name]["value"], expected)
            dependencies.append({"field": name, **dependency})
            if dependency["status"] == "resolved" and expected == 3:
                for index, offset in enumerate(SCHEMAS[3][3]):
                    value = _u(bytes.fromhex(dependency["raw_hex"]), offset, "u16")
                    if value is not None and value != 0xFFFF:
                        mid = model_id(bundle.index, value, 3)
                        dependencies.append({"kind": "model", "field": f"{name}.model_{index}", "id": mid,
                                             "status": "resolved" if mid else "index outside this bundle's models"})
                        if mid:
                            models.append(mid)
    if kind in RECORD_SIZES:
        payload = payload[: RECORD_SIZES[kind]]
    return marker(pid + ":marker", PLATFORM, f"ga_kind_{kind}", category, translation, placement["yaw"], fields,
                  source=source, evidence=[f"func_800DFA5C case {kind} -> {handler}"], confidence=confidence,
                  dependencies=dependencies, raw=payload, conditions=conditions, restrictions=restrictions, models=models)


# ------------------------------------------------------------- collision --

SNAP_MASK = 0x039BEF67  # func_800B1898: these flag bits snap the yaw to a right angle


def orientation_for(flags: int, yaw: int) -> tuple[str, int]:
    """func_800B1898 orientation: snap within 0x1000 of a right angle for SNAP_MASK flags."""

    if not flags & SNAP_MASK:
        return "oriented", yaw
    if flags == 0x800:
        return "axis_aligned", 0
    for right in (0, 0x4000, 0x8000, 0xC000):
        if min((yaw - right) & 0xFFFF, (right - yaw) & 0xFFFF) <= 0x1000:
            return "axis_aligned_snapped", right
    return "oriented", yaw


def model_box(pid: str, translation, yaw: int, bounds_: dict, flags: int, kind: int, top: float | None, source: dict) -> dict:
    orientation, effective = orientation_for(flags, yaw)
    vertical_top = bounds_["max_y"] if top is None else top
    return collision_record(
        pid + ":collision", "box", translation, effective, [bounds_["min_x"], 0, bounds_["min_z"]], [bounds_["max_x"], 0, bounds_["max_z"]],
        vertical={"status": "proven", "min": bounds_["min_y"], "max": vertical_top, "reference": "placement",
                  "evidence": "func_800DFA5C passes (model y0 + placement y, model y1 + placement y) as node y range; "
                              "800B33FC_grid_overlap_query compares node.y + y_min .. node.y + y_max"
                              + ("; kind 43 fixes the top at absolute 5000" if kind == 43 else "")},
        orientation=orientation, space="local", source=source, flags=flags, confidence="proven",
        evidence=["func_800DFA5C -> func_800B1898 with the model bounds", f"orientation rule: func_800B1898 (flag {flags:#x})"],
        extras={"source_yaw": yaw})


def explicit_box(pid: str, translation, yaw: int, values: list[int], flags: int, source: dict) -> dict:
    orientation, effective = orientation_for(flags, yaw)
    return collision_record(
        pid + ":collision", "box", translation, effective, [values[1], 0, values[3]], [values[4], 0, values[6]],
        vertical={"status": "proven", "min": values[2], "max": values[5], "reference": "placement",
                  "evidence": "func_800DFA5C case 30 passes node y = placement y with y range p[2]..p[5]; 800B33FC adds node.y"},
        orientation=orientation, space="local", source=source, flags=flags, confidence="proven",
        evidence=["func_800DFA5C case 30 modes 1/2 -> func_800B1898"], extras={"source_yaw": yaw})


def handler_box(pid: str, translation, yaw: int, kind: int, payload: bytes, source: dict) -> dict | None:
    """Grid boxes created by marker handlers with constant or descriptor extents."""

    if kind == 16:
        hx, hz = payload[1], payload[2]
        # The node is created with y = 0, so 0..100 is absolute; stored relative to the placement.
        flags, low, high = 0x10000, 0 - translation[1], 100 - translation[1]
        evidence = "func_800E9B50: func_800B1898(node y 0, -p[1], p[1], -p[2], p[2], 0, 100, flag 0x10000): absolute 0..100"
    elif kind == 27:
        size = 1 if payload[1] else 0
        hx = hz = (5, 150)[size]
        flags, low, high = 256, 0, 10
        evidence = "func_800F1B30: node y = placement y, half-extent D_80125AA0[size] (5 or 150), y 0..10, flag 256"
    else:
        return None
    orientation, effective = orientation_for(flags, yaw)
    return collision_record(
        pid + ":collision", "box", translation, effective, [-hx, 0, -hz], [hx, 0, hz],
        vertical={"status": "proven", "min": low, "max": high, "reference": "placement", "evidence": evidence},
        orientation=orientation, space="local", source=source, flags=flags, confidence="proven", evidence=[evidence],
        extras={"source_yaw": yaw})
