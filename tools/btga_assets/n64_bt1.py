"""BattleTanx (N64 USA) adapter into the neutral contract.

Evidence (RAM addresses in the BattleTanx image, ROM = RAM - 0x80071000 + 0x1000):

* 0x800865E0 fills the 28-slot stream table at 0x80125A68 with
  {world, world end, nav, nav end}; func_80088CF0 loads the world.
* World bundle: nine u32 offsets.  func_80088CF0 stores sections 0..7 and
  counts section 7 in 16-byte records (always empty in retail).
  - section 0: 0x2E0-byte header; words 0..7 copied to the level state
    (+0x10/+0x14 negated for func_80106B60), +0x20 flag bit 0, +0x27C group
    count, +0x280 group index read by func_80088670.
  - section 1: 20-byte groups {u16 count, u16 first placement, f32 x4}.
  - section 2: 28-byte placements: byte 0 flags (bit 0 = model via
    func_80088360, 0x10/0x40 conditions), byte 1, byte 2 subtype,
    byte 3 (0x80 flag), u32 type at +8, f32 x at +0xC, f32 z at +0x10,
    u16 yaw at +0x14, s16 model set at +0x16, s16 model at +0x18.
  - section 4: 4-byte model sets {u8 count, u8, u16 first model} (index < 64).
  - section 3: 12-byte models {u8 parts, u8, u16 first part, s16 x0, z0, x1, z1}
    (index < 450; footprint corners built at 0x800895E8 and rotated by
    func_800A7D10).
  - section 5: 4-byte parts {u8 refs, u8, u16 first ref} (index < 600).
  - section 6: 16-byte refs {u32 texture-DL offset, u16, u16 size,
    u32 geometry offset, u32 size} into the pools at 0x5A1110 and 0x3EA970
    (copied at 0x8008AA00 and 0x8008AB14).
* func_800A76BC builds the instance matrix: translation (x, 0, z), and
  m00 = m22 = cos, m20 = sin, m02 = -sin from the u16 yaw (func_800F3B40 /
  func_800F3AE0), exactly the Global Assault convention.
* func_80088670 dispatches marker types 10..25 through the table at
  0x80071768 (typed in n64_bt1_semantics).
* func_800DDA08 loads the nav bundle: word 2 packs {u16 points, u16 links},
  points are f32 (x, z) pairs, links 12-byte records.
"""

from __future__ import annotations

import hashlib
import json
import struct
from dataclasses import dataclass, field

from . import n64_gbi as gbi
from . import n64_sources as src
from .mesh import MeshBuilder, MeshError, bounds
from .n64_binary import BEReader, FormatError, raw_range, sha256
from .neutral import envelope

PLATFORM = "n64_bt1"
COORDINATES = {
    "neutral_from_source": "identity (x, 0, z ground plane; model y vertical)",
    "up_axis": "+Y",
    "handedness": "right",
    "front_face": "counter-clockwise (G_CULL_BACK)",
    "unit": "BattleTanx world unit (placement f32)",
    "evidence": {"rotation": "proven: func_800A76BC matrix from the u16 yaw", "front_face": "GBI convention"},
}
HEADER_SIZE = 0x2E0
GROUP = struct.Struct(">HHffff")
PLACEMENT = struct.Struct(">BBBBHHIffHhhH")
MODEL_SET = struct.Struct(">BBH")
MODEL = struct.Struct(">BBHhhhh")
PART = struct.Struct(">BBH")
REFERENCE = struct.Struct(">IHHII")
LIMITS = {"model_sets": 64, "models": 450, "parts": 600, "references": 3600}


def content_id(kind: str, rom_sha256: str, content) -> str:
    text = json.dumps(content, sort_keys=True, separators=(",", ":"))
    return f"{PLATFORM}:{kind}:{hashlib.sha256(f'{rom_sha256}|{kind}|{text}'.encode()).hexdigest()[:24]}"


def parse_world(data: bytes, name: str = "world") -> dict:
    reader = BEReader(data, name)
    if len(data) < 36:
        raise FormatError(f"{name}: shorter than its nine-offset header")
    offsets = [reader.u32(4 * i) for i in range(9)]
    if offsets[0] != 36 or offsets[8] != len(data) or offsets != sorted(offsets):
        raise FormatError(f"{name}: section offsets do not cover the decoded data")
    if offsets[1] - offsets[0] != HEADER_SIZE:
        raise FormatError(f"{name}: header is {offsets[1] - offsets[0]} bytes, expected {HEADER_SIZE}")
    header = data[offsets[0] : offsets[1]]
    hreader = BEReader(header, name + " header")

    def table(section: int, record: struct.Struct, what: str) -> list[tuple]:
        count = reader.records(offsets[section], offsets[section + 1], record.size, what)
        return [record.unpack_from(data, offsets[section] + i * record.size) for i in range(count)]

    groups = table(1, GROUP, "groups")
    placements = table(2, PLACEMENT, "placements")
    models = table(3, MODEL, "models")
    model_sets = table(4, MODEL_SET, "model sets")
    parts = table(5, PART, "parts")
    references = table(6, REFERENCE, "references")
    trailing = reader.records(offsets[7], offsets[8], 16, "section 7")
    group_count = hreader.u32(0x27C)
    group_index = hreader.s32(0x280)
    # 0x800878C8 walks group_count records from record 0 when the header's
    # +0x280 word is -1, otherwise from record 1.
    first_active = 0 if group_index == -1 else 1
    if first_active + group_count > len(groups):
        raise FormatError(f"{name}: header group count {group_count} exceeds the {len(groups)} group records")
    for count, first, *_ in groups:
        if first + count > len(placements):
            raise FormatError(f"{name}: group selects placements past the section")
    for kind, rows, limit_name, target in (("model set", model_sets, "model_sets", models), ("model", models, "models", parts), ("part", parts, "parts", references)):
        if len(rows) > LIMITS[limit_name]:
            raise FormatError(f"{name}: {len(rows)} {kind}s exceed the loader limit {LIMITS[limit_name]}")
        for row in rows:
            if row[2] + row[0] > len(target):
                raise FormatError(f"{name}: {kind} range exceeds its target section")
    for row in placements:
        if row[10] != -1 and not 0 <= row[10] < len(model_sets):
            raise FormatError(f"{name}: placement model set {row[10]} is outside the section")
        if row[11] != -1 and not 0 <= row[11] < len(models):
            raise FormatError(f"{name}: placement model {row[11]} is outside the section")
    return {
        "offsets": offsets,
        "header": {
            "raw": raw_range(data, offsets[0], HEADER_SIZE),
            "bounds_a": [hreader.f32(4 * i) for i in range(4)],
            "bounds_b": [hreader.f32(0x10 + 4 * i) for i in range(4)],
            "flags": hreader.u32(0x20),
            "group_count": group_count,
            "group_index": hreader.s32(0x280),
            "unknown_ranges": [raw_range(header, 0x24, 0x27C - 0x24), raw_range(header, 0x284, HEADER_SIZE - 0x284)],
        },
        "groups": [{"count": g[0], "first": g[1], "bounds": list(g[2:]), "active": first_active <= i < first_active + group_count,
                    "marker_group": group_index != -1 and i == group_index}
                   for i, g in enumerate(groups)],
        "placements": [
            {"flags": p[0], "byte1": p[1], "subtype": p[2], "byte3": p[3], "word4": p[4], "word6": p[5], "type": p[6],
             "x": p[7], "z": p[8], "yaw": p[9], "model_set": p[10], "model": p[11], "word1a": p[12]}
            for p in placements
        ],
        "models": [{"part_count": m[0], "byte1": m[1], "first_part": m[2], "footprint": list(m[3:])} for m in models],
        "model_sets": [{"count": s[0], "byte1": s[1], "first_model": s[2]} for s in model_sets],
        "parts": [{"reference_count": p[0], "byte1": p[1], "first_reference": p[2]} for p in parts],
        "references": [{"texture_offset": r[0], "word4": r[1], "texture_size": r[2], "geometry_offset": r[3], "geometry_size": r[4]} for r in references],
        "section7_records": trailing,
    }


def parse_nav(data: bytes, name: str = "nav") -> dict:
    """Nav bundle as func_800DDA08 relocates it."""

    reader = BEReader(data, name)
    words = [reader.u32(4 * i) for i in range(10)]
    points, links = words[2] >> 16, words[2] & 0xFFFF
    for index in (1, 3, 4, 5, 6, 7):
        if not 0x28 <= words[index] <= len(data):
            raise FormatError(f"{name}: header word {index} offset {words[index]:#x} is outside the bundle")
    if words[3] + points * 8 > words[4]:
        raise FormatError(f"{name}: {points} points overrun the link section")
    if words[4] + links * 12 > words[6]:
        raise FormatError(f"{name}: {links} links overrun the following section")
    return {
        "header_words": words,
        "points": [[reader.f32(words[3] + 8 * i), reader.f32(words[3] + 8 * i + 4)] for i in range(points)],
        "links": [[reader.u16(words[4] + 12 * i + 2 * k) for k in range(6)] for i in range(links)],
        "link_fields": "unknown (six u16 per 12-byte record, retained raw)",
        "tables": [raw_range(data, words[6], words[7] - words[6]), raw_range(data, words[7], len(data) - words[7])],
        "point_evidence": "count proven (word 2); f32 (x, z) layout corroborated by in-bounds coordinates",
    }


@dataclass
class Level:
    slot: int
    world_range: tuple[int, int]
    data: bytes
    world: dict
    nav: dict | None
    nav_range: tuple[int, int] | None
    problems: list[str] = field(default_factory=list)


def load_level(rom: bytes, slot: int) -> Level:
    table = src.bt1_stream_table(rom)
    if not 0 <= slot < len(table) or table[slot]["world"] is None:
        raise FormatError(f"BattleTanx stream slot {slot} has no world")
    start, end = table[slot]["world"]
    data = src.decode_stream(rom, start, end)
    world = parse_world(data, f"slot {slot}")
    nav = None
    if table[slot]["nav"]:
        nav = parse_nav(src.decode_stream(rom, *table[slot]["nav"]), f"slot {slot} nav")
    return Level(slot, (start, end), data, world, nav, table[slot]["nav"])


def _pool(rom: bytes, pool: str, offset: int, size: int) -> bytes:
    base = src.BT1_POOLS[pool]
    if size <= 0 or base + offset + size > len(rom):
        raise FormatError(f"{pool} reference {offset:#x}+{size:#x} leaves the ROM")
    return rom[base + offset : base + offset + size]


def model_record(rom: bytes, rom_sha: str, level: Level, model_index: int, textures: dict[str, dict]) -> dict:
    world = level.world
    model = world["models"][model_index]
    parts = []
    for part_index in range(model["first_part"], model["first_part"] + model["part_count"]):
        part = world["parts"][part_index]
        parts.append(world["references"][part["first_reference"] : part["first_reference"] + part["reference_count"]])
    record_id = content_id("model", rom_sha, parts)
    builder = MeshBuilder()
    reference_records = []
    for part_index, refs in enumerate(parts):
        builder.begin_lod(part_index, None)
        for ref in refs:
            name = f"bt1 ref g{ref['geometry_offset']:#x}"
            state = gbi.RenderState()
            texture_payload = _pool(rom, "texture", ref["texture_offset"], ref["texture_size"])
            _, texture_commands = gbi.find_stream(texture_payload, gbi.F3DEX)
            gbi.run_state(texture_payload, texture_commands, gbi.F3DEX, state, name)
            geometry_payload = _pool(rom, "geometry", ref["geometry_offset"], ref["geometry_size"])
            _, geometry_commands = gbi.find_stream(geometry_payload, gbi.F3DEX, {0x04, 0xBF, 0xB1})
            mesh = gbi.run_geometry(geometry_payload, geometry_commands, gbi.F3DEX, name)
            key = [ref["texture_offset"], ref["texture_size"]]
            texture_id = content_id("texture", rom_sha, key)
            if texture_id not in textures:
                described = gbi.texture_from_state(state, texture_payload, name)
                if described is not None and "rgba" in described:
                    rgba = described["rgba"]
                    alpha = {rgba[i] for i in range(3, len(rgba), 4)}
                    textures[texture_id] = {
                        "id": texture_id, "width": described["width"], "height": described["height"], "format": described["format"],
                        "wrap": {"s": "clamp" if described["wrap"]["s"] & 2 else ("mirror" if described["wrap"]["s"] & 1 else "repeat"),
                                 "t": "clamp" if described["wrap"]["t"] & 2 else ("mirror" if described["wrap"]["t"] & 1 else "repeat")},
                        "alpha": "opaque" if alpha == {255} else ("mask" if alpha <= {0, 255} else "blend"),
                        "rgba": rgba,
                        "source": {"platform": PLATFORM, "texture_pool": key, "image_address": described["image_address"], "palette": described["palette"],
                                   "tile_origin": described["tile_origin"], "wrap_raw": described["wrap"]},
                    }
                else:
                    textures[texture_id] = {"id": texture_id, "status": (described or {}).get("status", "no texture load"), "source": {"texture_pool": key}}
            texture = textures[texture_id] if "rgba" in textures[texture_id] else None
            mode = state.geometry_mode
            cull_back, cull_front = bool(mode & gbi.F3DEX.cull_back), bool(mode & gbi.F3DEX.cull_front)
            lit = bool(mode & gbi.F3DEX.lighting)
            material = builder.material(
                {
                    "id": f"{texture_id if texture else 'untextured'}|cull{int(cull_front)}{int(cull_back)}|lit{int(lit)}",
                    "texture": texture_id if texture else None,
                    "double_sided": not cull_back and not cull_front,
                    "alpha_mode": "MASK" if texture and texture["alpha"] == "mask" else ("BLEND" if texture and texture["alpha"] == "blend" else "OPAQUE"),
                    "geometry_mode": f"{mode:08x}",
                    "othermode_h": f"{state.othermode_h:08x}",
                    "othermode_l": f"{state.othermode_l:08x}",
                    "combine": [f"{state.combine[0]:06x}", f"{state.combine[1]:08x}"],
                    "vertex_colour": "normal (lit)" if lit else "shade RGBA, 255 = 1.0",
                }
            )
            base = len(builder.positions)
            for vertex in mesh.vertices:
                builder.position(vertex["position"])
            tile = state.tiles[state.texture_tile]
            from .n64_btga import _st_to_uv

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
            reference_records.append({"part": part_index, **ref, "vertex_count": len(mesh.vertices), "triangle_count": len(mesh.triangles),
                                      "texture": texture_id if texture else None, "unknown_commands": state.unknown})
        builder.end_lod()
    built = builder.build()
    record = envelope("model", record_id, {"platform": PLATFORM, "rom_sha256": rom_sha, "slot": level.slot, "world_rom_range": list(level.world_range), "model_index": model_index})
    record.update({"coordinate_system": COORDINATES, "footprint": model["footprint"], "part_semantics": "parts drawn per placement; LOD role unproven",
                   "references": reference_records, "mesh": built, "bounds": bounds(built)})
    return record


def scene_record(rom: bytes, rom_sha: str, level: Level, textures: dict[str, dict]) -> tuple[dict, dict[str, dict]]:
    from . import n64_bt1_semantics as sem
    from .semantics import SCENE_SCHEMA_VERSION

    world = level.world
    models: dict[str, dict] = {}
    cache: dict[int, str] = {}
    instances, collision, markers, unresolved = [], [], [], []
    placement_base = world["offsets"][2]

    def model_id(index: int) -> str:
        if index not in cache:
            record = model_record(rom, rom_sha, level, index, textures)
            models[record["id"]] = record
            cache[index] = record["id"]
        return cache[index]

    def raw(index: int) -> bytes:
        return level.data[placement_base + 28 * index : placement_base + 28 * (index + 1)]

    for group_index, group in enumerate(world["groups"]):
        if group["marker_group"]:
            # func_80088670 walks only the header-selected group, dispatching types 10..25.
            for index in range(group["first"], group["first"] + group["count"]):
                p = world["placements"][index]
                source = {"group": group_index, "placement_index": index, "placement_offset": placement_base + 28 * index, "type": p["type"]}
                record = sem.marker_group_record(level.slot, index, p, raw(index), source)
                record["restrictions"].update({k: v for k, v in sem.restrictions(p).items() if k != "disabled_when"})
                markers.append(record)
            continue
        if not group["active"]:
            continue
        for index in range(group["first"], group["first"] + group["count"]):
            p = world["placements"][index]
            pid = f"{PLATFORM}:placement:{level.slot}:{index}"
            source = {"group": group_index, "placement_index": index, "placement_offset": placement_base + 28 * index, "type": p["type"]}
            base = {"id": pid, "translation": [p["x"], 0.0, p["z"]], "yaw": p["yaw"],
                    "extras": {"type": p["type"], "group": group_index, "flags": p["flags"], "subtype": p["subtype"], "raw": p,
                               "restrictions": sem.restrictions(p)}}
            if p["model"] >= 0:
                instances.append({**base, "model": model_id(p["model"]), "category": "model"})
                shape, note = sem.placement_collision(level.slot, index, p, world, {**source, "model": model_id(p["model"])})
                if shape is not None:
                    shape["extras"]["restrictions"] = sem.restrictions(p)
                    collision.append(shape)
                if note is not None:
                    unresolved.append(note)
            if p["model_set"] >= 0:
                entry = world["model_sets"][p["model_set"]]
                base["extras"]["model_set_models"] = [model_id(i) for i in range(entry["first_model"], entry["first_model"] + entry["count"])]
            typed = sem.active_group_marker(level.slot, index, p, raw(index), source)
            if typed is not None:
                markers.append(typed)
    nav = sem.navigation(level.nav, level.nav_range) if level.nav else None
    scene = envelope("scene", f"{PLATFORM}:scene:{level.slot}:{sha256(level.data)[:16]}",
                     {"platform": PLATFORM, "rom_sha256": rom_sha, "slot": level.slot, "world_rom_range": list(level.world_range), "decoded_sha256": sha256(level.data)})
    scene.update(
        {
            "semantics_version": SCENE_SCHEMA_VERSION,
            "coordinate_system": COORDINATES,
            "instances": instances,
            "collision": collision,
            "markers": markers,
            "environment": [{"header_raw": world["header"]["raw"], "status": "colour blocks at header +0x24..+0xD0 copied by func_80088CF0; meaning untraced"}],
            "bounds": {"header_bounds_a": world["header"]["bounds_a"], "header_bounds_b": world["header"]["bounds_b"],
                       "map": sem.map_bounds(world["header"]),
                       "groups": [{"group": i, "bounds": g["bounds"]} for i, g in enumerate(world["groups"])]},
            "navigation": nav,
            "minimap": {"status": "unsupported", "selections": [], "note": "no BattleTanx minimap source traced"},
            "music": {"status": "unsupported"},
            "unresolved": unresolved,
            "models": sorted(models),
        }
    )
    return scene, models
