"""BattleTanx (N64) world semantics: collision, map bounds, typed markers, navigation.

Addresses are RAM in the BattleTanx image (ROM = RAM - 0x80071000 + 0x1000).

Collision (proven)
    The placement loop at 0x80089370..0x8008A758 registers footprints with
    func_80106D18(owner, x, z, x0, x1, z0, z1, mask, yaw).  The collision
    node (40-byte records at 0x803B8248) and its geometry helper
    func_80109268 store only x, z, the four footprint extents, the yaw and
    a radius; nothing vertical.  Collision is therefore a planar oriented
    rectangle: no height exists to recover.
      * types 1, 19, 27 grow the model footprint by the f32 at 0x80071894
        (1.0) on every side; type 1 uses mask 0xF85F, types 19/27 mask 0x1A;
      * type 26 (owner 1) and type 30 (owner 2) use the bare footprint,
        mask 0xF85F;
      * type 8 registers through func_800ECC1C (an actor at y 30.0 with mask
        0xF85F); types 6 (subtype bit 0x10), 9 and 15 pass the model
        footprint to func_800F05F4, func_800907CC and func_80091D6C;
      * types 3, 5, 20 and 31 create actors whose footprint arguments are
        set inside helpers not traced here.
    The footprint corners and radius computed at 0x800895E8..0x80089A74 are
    never stored (dead code).
    func_80106B60 (called at 0x80089064) accepts nodes only inside
    |x| <= 500 - header.f32[+0x10] and |z| <= 500 - header.f32[+0x14], and
    buckets them in 2048-unit cells (sra 11).

Mode gating (proven)
    flags bits 0x10/0x40 consult func_8007C6D8; bit 0x2 compares the team
    table 0x801260A4[(flags & 0xC) >> 2] with func_8007AAB0(); when
    func_8007AAA0() >= 3 the table at 0x80071898 rewrites types 3, 6, 9, 15
    and 18 to type 7 and disables them.

Marker group (func_80088670, jump table 0x80071768, types 10..25)
    25 per-team slots (<= 5 per team), 22 team start, 21/23/24 created only
    when func_8007AAA0() == 1, 10 point lists, 11..20 no-ops.
"""

from __future__ import annotations

import struct

from .semantics import collision as collision_record
from .semantics import field, marker

PLATFORM = "n64_bt1"
WALL_MARGIN = 1.0  # f32 at 0x80071894
GRID_MARGIN = 500
DIRECT = {  # type: (owner, mask, margin)
    1: (0, 0xF85F, WALL_MARGIN),
    19: (0, 0x1A, WALL_MARGIN),
    27: (0, 0x1A, WALL_MARGIN),
    26: (1, 0xF85F, 0.0),
    30: (2, 0xF85F, 0.0),
}
ACTOR_FOOTPRINT = {8: "func_800ECC1C", 9: "func_800907CC", 15: "func_80091D6C", 6: "func_800F05F4"}
ACTOR_UNTRACED = {3: "func_800EF984", 5: "func_80091E98", 20: "func_80091E98", 31: "func_80091E98"}
DISABLED_WITH_THREE_PLUS = {3, 6, 9, 15, 18}
NO_VERTICAL = {"status": "none_stored", "min": None, "max": None, "reference": None,
               "evidence": "func_80106D18 / func_80109268 store x, z, extents, yaw and radius only; the collision test is planar"}


def map_bounds(header: dict) -> dict:
    # func_80106B60 receives trunc.w.s(-h[+0x10]) and trunc.w.s(-h[+0x14]).
    hx = int(-header["bounds_b"][0]) + GRID_MARGIN
    hz = int(-header["bounds_b"][1]) + GRID_MARGIN
    return {"x": [-hx, hx], "z": [-hz, hz], "status": "proven",
            "evidence": "0x80089064 calls func_80106B60(-h[+0x10], -h[+0x14]); it stores value + 500 as the half-extents "
                        "func_80106D18 tests before inserting a collision node"}


def restrictions(placement: dict) -> dict:
    out = {}
    if placement["type"] in DISABLED_WITH_THREE_PLUS:
        out["disabled_when"] = "func_8007AAA0() >= 3 (table 0x80071898 rewrites the type to 7)"
    flags = placement["flags"]
    if flags & 0x10 or flags & 0x40:
        out["flag_conditions"] = {"bit_0x10": bool(flags & 0x10), "bit_0x40": bool(flags & 0x40), "consults": "func_8007C6D8"}
    if flags & 0x2:
        out["team_gate"] = {"team_table_index": (flags & 0xC) >> 2, "table": "0x801260A4", "compared_with": "func_8007AAB0()"}
    return out


def footprint_shape(pid: str, placement: dict, footprint: list[int], margin: float, mask, owner, source: dict, evidence: list[str],
                    confidence: str = "proven") -> dict:
    x0, z0, x1, z1 = footprint
    return collision_record(pid + ":collision", "footprint", [placement["x"], 0.0, placement["z"]], placement["yaw"],
                            [x0 - margin, 0, z0 - margin], [x1 + margin, 0, z1 + margin], vertical=dict(NO_VERTICAL),
                            orientation="oriented", space="local", source=source, flags=mask, confidence=confidence, evidence=evidence,
                            extras={"owner": owner, "margin": margin})


def placement_collision(level_slot: int, index: int, placement: dict, world: dict, source: dict) -> tuple[dict | None, dict | None]:
    """(collision shape, unresolved note) for one active placement."""

    shape, note = _placement_collision(level_slot, index, placement, world, source)
    if shape is not None:
        bounds_ = map_bounds(world["header"])
        x, z = int(placement["x"]), int(placement["z"])
        inside = bounds_["x"][0] <= x <= bounds_["x"][1] and bounds_["z"][0] <= z <= bounds_["z"][1]
        shape["extras"]["registered"] = inside
        if not inside:
            shape["extras"]["rejected_because"] = "func_80106D18 returns -1 for positions outside the map half-extents; no node is created"
    return shape, note


def _placement_collision(level_slot: int, index: int, placement: dict, world: dict, source: dict) -> tuple[dict | None, dict | None]:
    pid = f"{PLATFORM}:placement:{level_slot}:{index}"
    kind = placement["type"]
    if placement["model"] < 0:
        return None, None
    footprint = world["models"][placement["model"]]["footprint"]
    if kind in DIRECT:
        owner, mask, margin = DIRECT[kind]
        return footprint_shape(pid, placement, footprint, margin, mask, owner, source,
                               [f"type {kind} -> func_80106D18 (owner {owner}, mask {mask:#x}, margin {margin})"]), None
    if kind in ACTOR_FOOTPRINT:
        if kind == 6 and not placement["subtype"] & 0x10:
            return None, None
        mask = 0xF85F if kind == 8 else None
        shape = footprint_shape(pid, placement, footprint, 0.0, mask, "actor", source,
                                [f"type {kind} -> {ACTOR_FOOTPRINT[kind]} with the model footprint and yaw -> func_80106D18"])
        if mask is None:
            shape["extras"]["mask_status"] = "set inside the helper; not traced"
        return shape, None
    if kind in ACTOR_UNTRACED:
        return None, {"placement": index, "type": kind, "reason": f"actor collision created inside {ACTOR_UNTRACED[kind]}; extents untraced"}
    return None, None


def _bits(value: int, shift: int, width: int) -> int:
    return (value >> shift) & ((1 << width) - 1)


def marker_group_record(level_slot: int, index: int, p: dict, raw: bytes, source: dict) -> dict:
    """Typed record for one placement of the header-selected marker group."""

    pid = f"{PLATFORM}:marker:{level_slot}:{index}"
    kind = p["type"]
    translation = [p["x"], 0.0, p["z"]]
    word4 = p["word4"]
    common = {"source": source, "raw": raw, "record_size": 28}
    if kind == 25:
        fields = {
            "team": field(p["subtype"] >> 4, "u8", 2, "proven", "subtype >> 4: team list at level state +0x370 (at most 5 entries each)", legal=(0, 15)),
            "slot_kind": field(p["subtype"] & 0xF, "u8", 2, "proven", "subtype & 0xF stored at entry +0xC", legal=(0, 15)),
            "parameter": field(word4 & 0xFF, "u8", 5, "proven", "byte 5 stored at entry +0x10"),
            "parameter_bits": field(_bits(word4, 12, 2), "u8", 4, "proven", "bits 12..13 of the u16 at +4, stored at entry +0x11", legal=(0, 3)),
        }
        return marker(pid, PLATFORM, "bt1_marker_25", "team_spawn_slot", translation, p["yaw"], fields,
                      evidence=["func_80088670 type 25 (0x800887B0): entry at state +0x380 + team * 0x78 + n * 0x18"], confidence="proven",
                      restrictions={"max_per_team": 5}, **common)
    if kind == 22:
        fields = {
            "requires_flag": field(word4 & 1, "u8", 4, "proven", "bit 0: created only if func_8007C728() and 0x80134E67 allow", legal=(0, 1)),
            "team_index": field(_bits(word4, 1, 2), "u8", 4, "proven", "bits 1..2 index team table 0x801260A4; created only below func_8007AAB0()", legal=(0, 3)),
            "variant": field(_bits(word4, 3, 2), "u8", 4, "proven", "bits 3..4 passed to func_800A1408", legal=(0, 3)),
            "byte5_bit0": field(raw[5] & 1, "u8", 5, "proven", "bit 0 of byte 5 passed to func_800A1408 unless func_8007C6D8()", legal=(0, 1)),
        }
        return marker(pid, PLATFORM, "bt1_marker_22", "team_start", translation, p["yaw"], fields,
                      evidence=["func_80088670 type 22 (0x800889D0) -> func_800A1408(team colour, team, variant, yaw, position)"],
                      confidence="proven", **common)
    if kind == 21:
        fields = {
            "generator_parameter": field(word4, "u16", 4, "proven", "second argument of func_8010D480"),
            "subtype": field(p["subtype"], "u8", 2, "proven", "last argument of func_8010D480"),
        }
        return marker(pid, PLATFORM, "bt1_marker_21", "tank_generator", translation, p["yaw"], fields,
                      evidence=["func_80088670 type 21 (0x80088AA0) -> func_8010D480(0, u16 +4, x, z, yaw, subtype)",
                                "role 'tank generator' inferred: Chicago Bonus (scenario 8, mode 7, kill target 5) holds three"],
                      confidence="proven", restrictions={"only_when": "func_8007AAA0() == 1"}, **common)
    if kind == 23:
        return marker(pid, PLATFORM, "bt1_marker_23", "single_player_point", translation, p["yaw"], {},
                      evidence=["func_80088670 type 23 (0x8008899C) -> func_8010DAA0(position, -1)"], confidence="proven",
                      restrictions={"only_when": "func_8007AAA0() == 1"}, **common)
    if kind == 24:
        fields = {"parameter": field(struct.unpack(">h", raw[4:6])[0], "s16", 4, "proven", "third argument of func_800A7550")}
        return marker(pid, PLATFORM, "bt1_marker_24", "single_player_effect", translation, p["yaw"], fields,
                      evidence=["func_80088670 type 24 (0x80088AE0) -> func_800A7550(x, z, s16 +4)"], confidence="proven",
                      restrictions={"only_when": "func_8007AAA0() == 1"}, **common)
    if kind == 10:
        fields = {"subtype": field(p["subtype"], "u8", 2, "proven", "22 adds the point to a list of at most 100; 5..16 select func_800911A0 arguments")}
        return marker(pid, PLATFORM, "bt1_marker_10", "point_list_entry" if p["subtype"] == 22 else "marker_10", translation, p["yaw"], fields,
                      evidence=["func_80088670 type 10 (0x8008883C)"], confidence="proven", **common)
    note = "types 11..20 fall through to the loop end (0x80088B08)" if 11 <= kind <= 20 else "type outside the 10..25 table"
    return marker(pid, PLATFORM, f"bt1_marker_{kind}", "inactive_in_marker_group", translation, p["yaw"], {},
                  evidence=[f"func_80088670: {note}"], confidence="proven", **common)


def active_group_marker(level_slot: int, index: int, p: dict, raw: bytes, source: dict) -> dict | None:
    """Types 16 and 17 in an active group store a point per subtype (0x80089C7C / 0x80089CD4)."""

    if p["type"] not in (16, 17):
        return None
    which = "a" if p["type"] == 16 else "b"
    fields = {"slot": field(p["subtype"], "u8", 2, "proven", "index of the 20-byte record at level state +0xC8 (flag set, point stored)")}
    return marker(f"{PLATFORM}:marker:{level_slot}:{index}", PLATFORM, f"bt1_type_{p['type']}", f"team_point_{which}",
                  [p["x"], 0.0, p["z"]], p["yaw"], fields, source=source, raw=raw, record_size=28,
                  evidence=[f"placement loop type {p['type']}: state +0xC8 + subtype * 20 gets flag 1 and "
                            + ("x at +0xD0, z at +0xCC" if which == "a" else "x at +0xD8, z at +0xD4"),
                            "role 'team base' inferred from earlier analysis, not stated by code"], confidence="proven",
                  restrictions=restrictions(p))


def navigation(nav: dict, rom_range) -> dict:
    points = [{"index": i, "x": x, "z": z} for i, (x, z) in enumerate(nav["points"])]
    links = []
    for i, link in enumerate(nav["links"]):
        links.append({"index": i, "targets": [link[0], link[2], link[4]], "values": [link[1], link[3], link[5]], "raw": link})
    return {
        "points": points,
        "links": links,
        "point_count": len(points),
        "link_fields": {
            "targets": {"offsets": [0, 4, 8], "evidence": "corroborated: below the point count in every link of all 18 navigation bundles"},
            "values": {"offsets": [2, 6, 10], "evidence": "unresolved: values exceed the point count; role untraced"},
        },
        "tables": nav["tables"],
        "rom_range": list(rom_range),
        "evidence": nav["point_evidence"],
    }
