"""PS1 Global Assault (SLUS_010.44) world semantics: floor lattice, collision, typed markers.

Floor (.FLB), proven
    * 0x80039F80..0x8003A05C store the viewer position rounded >> 12
      (model units) at 0x800D5B38; floor vertices are 4 * coord - camera
      (0x8003B094..0x8003B0D8) and a quad edge is 0x200, so lattice points
      are 512 model units apart.
    * 0x8003BADC maps an object position (its 20.12 coordinates >> 12, from
      callers such as 0x8002CB08) to a lattice point:
          col = ((256 - x) >> 9) + dim / 2,  row = ((z + 256) >> 9) + dim / 2
      i.e. the nearest point, with x mirrored.  It clears bits 0..14 of
      that point (callers run when the object above is destroyed).
    * Values are per lattice point, not per area: 0x8003AF64 samples a
      point's colour for each quad corner (Gouraud).
    * Bits 0..6 form the near floor vertex colour (0x8003B100); byte 3
      (bits 24..31) of neighbouring points forms the far floor colour
      (0x8003B828..0x8003B864, depth-cued by func_8008BDF0).
    * No height, material, walkability or blocking test reads the lattice in
      any traced path; those meanings are not established.

Collision, proven
    Kinds 0, 1, 11, 12, 14, 22 and 43 (table 0x800BD6A4) read the CM01
    footprint {x_a, z_a, x_b, z_b} and call 0x800249A0, which rotates the
    four corners with y = 0 by the placement yaw and keeps
    [min_x, min_z, max_x, max_z]; the dispatcher stores those << 14 plus the
    20.12 position at object +0x2C..+0x38.  The result is a world-aligned
    rectangle with no vertical extent.
"""

from __future__ import annotations

import math
import struct

from .semantics import collision as collision_record
from .semantics import field, marker

PLATFORM = "ps1"
LATTICE_SPACING = 512
FOOTPRINT_KINDS = {0: 2, 1: 2, 11: 2, 12: 4, 14: 2, 22: 2, 43: 2}  # kind -> offset of the u16 model index
NO_OP_KINDS = {2, 9, 18, 19, 23, 25, 33, 34, 36, 39, 40, 41}  # table entries jump straight to the loop tail 0x80026168
HANDLERS = {3: "func_800263F0", 4: "func_8002A914", 5: "func_8002D6A0", 6: "func_80024720", 7: "func_80029BF0", 8: "func_80037380",
            10: "func_8002E568", 13: "func_80034CD4", 15: "func_80034E60", 16: "func_80034250", 17: "func_8008AD60/func_8008ACC0",
            20: "func_80032A68", 21: "func_8002E754", 24: "func_8002CF3C", 26: "func_80030DA4", 27: "func_80027F38", 28: "func_80035DB8",
            29: "func_800371FC", 30: "inline at 0x80025BD0", 31: "func_80033DFC", 32: "func_8003159C", 35: "func_80033B28",
            37: "func_800247DC/func_800248A0", 38: "func_800248A0", 42: "func_80024D80"}
NO_VERTICAL = {"status": "none_stored", "min": None, "max": None, "reference": None,
               "evidence": "0x800249A0 rotates the footprint with y = 0 and the object stores only [min_x, min_z, max_x, max_z] at +0x2C..+0x38"}


def lattice_point(col: int, row: int, dimension: int) -> tuple[int, int]:
    """Model-unit (x, z) of lattice point (col, row)."""

    return -LATTICE_SPACING * (col - dimension // 2), LATTICE_SPACING * (row - dimension // 2)


def lattice_index(x: float, z: float, dimension: int) -> tuple[int, int]:
    """0x8003BADC: nearest lattice point (col, row) of a model-unit position (may lie outside the grid)."""

    return ((256 - int(x)) >> 9) + dimension // 2, ((int(z) + 256) >> 9) + dimension // 2


def floor_layer(flb: dict, file: str, sha: str) -> dict:
    d = flb["dimension"]
    (x_hi, z_lo), (x_lo, z_hi) = lattice_point(0, 0, d), lattice_point(d - 1, d - 1, d)
    return {
        "status": "proven",
        "file": file,
        "file_sha256": sha,
        "dimension": d,
        "spacing": LATTICE_SPACING,
        "point_position": "x = -512 * (col - dimension/2), z = 512 * (row - dimension/2); value index = row * dimension + col",
        "lookup": "col = ((256 - x) >> 9) + dimension/2, row = ((z + 256) >> 9) + dimension/2 (0x8003BADC)",
        "extent": {"x": [x_lo, x_hi], "z": [z_lo, z_hi]},
        "cells": flb["cells"],
        "bits": {
            "0-6": {"evidence": "proven", "meaning": "near floor vertex colour: ((c & 0x7C) << 19) | ((c & 0x3E) << 11) | ((c & 0x1F) << 3) (0x8003B100)"},
            "0-14": {"evidence": "proven", "meaning": "cleared (cell &= 0xFFFF8000) by 0x8003BADC under an object that is destroyed"},
            "24-31": {"evidence": "proven", "meaning": "far floor colour channel: byte 3 of neighbouring points packed into a colour (0x8003B828)"},
            "15-21": {"evidence": "unresolved", "meaning": "raw; corroborated correlation only: set 3-4x more often under placed models"},
            "7-14, 22-23": {"evidence": "unresolved", "meaning": "raw"},
        },
        "not_established": ["height", "walkable/blocked", "material", "navigation"],
        "header_word_2": flb["header_word_2"],
        "unreachable_tail": flb["unreachable_tail"],
    }


def footprint_aabb(footprint: dict, yaw: int) -> tuple[list[float], list[float]]:
    """Rotate the four footprint corners (x4 model units) and keep the axis-aligned bounds."""

    theta = (yaw & 0xFFFF) * 2 * math.pi / 65536
    c, s = math.cos(theta), math.sin(theta)
    xs, zs = (footprint["x_a"], footprint["x_b"]), (footprint["z_a"], footprint["z_b"])
    corners = [(4 * x, 4 * z) for x in xs for z in zs]
    rotated = [(x * c + z * s, -x * s + z * c) for x, z in corners]
    return [min(p[0] for p in rotated), 0, min(p[1] for p in rotated)], [max(p[0] for p in rotated), 0, max(p[1] for p in rotated)]


def collision_for(placement: dict, model_record: dict, model_id: str) -> dict | None:
    if placement["kind"] not in FOOTPRINT_KINDS:
        return None
    footprint = model_record["header"]["footprint"]
    lo, hi = footprint_aabb(footprint, placement["word_6"])
    return collision_record(
        placement["id"] + ":collision", "aabb", placement["translation"], 0, lo, hi, vertical=dict(NO_VERTICAL),
        orientation="axis_aligned_world", space="local",
        source={"placement_index": placement["index"], "kind": placement["kind"], "model": model_id},
        evidence=[f"kind {placement['kind']} (table 0x800BD6A4) -> 0x800249A0 footprint AABB; stored at object +0x2C..+0x38",
                  "computed here in floating point; the game rotates in 12-bit fixed point"],
        confidence="proven", extras={"source_yaw": placement["word_6"]})


def model_index_for(placement: dict, raw: bytes) -> int | None:
    offset = FOOTPRINT_KINDS.get(placement["kind"])
    if offset is None or len(raw) < offset + 2:
        return None
    value = struct.unpack_from("<H", raw, offset)[0]
    return None if value == 0xFFFF else value


def scaled_extent(value: int) -> int:
    """0x80025BD0: (value << 6) * 0x38E38E39 >> 32 >> 4, rounded toward zero = trunc(value * 64 / 72)."""

    return int(value * 64 / 72)


def lane_bounds(placement: dict, raw: bytes) -> dict | None:
    """Kind 30 mode 0: lane bounds written to the object at 0x800D2FE4 (+0x14..+0x1A)."""

    if placement["kind"] != 30 or len(raw) < 14 or raw[1] != 0:
        return None
    x, _, z = placement["position_raw"]
    e = [scaled_extent(struct.unpack_from("<h", raw, o)[0]) for o in (2, 6, 8, 12)]
    return {"x": [4 * (x + e[0]), 4 * (x + e[2])], "z": [4 * (z + e[1]), 4 * (z + e[3])], "status": "proven",
            "source": {"placement_index": placement["index"]},
            "evidence": "0x80025BD0 mode 0: placement (model units / 4) + trunc(p * 64 / 72) for p at +2/+8 (x) and +6/+12 (z); "
                        "stored at 0x800D2FE4 object +0x14/+0x18/+0x16/+0x1A; shown here x4 in model units; the consumer of these "
                        "bounds is untraced and they do not enclose the level (about 10% of placements fall inside)"}


def typed_marker(placement: dict, raw: bytes) -> dict | None:
    kind = placement["kind"]
    if kind in FOOTPRINT_KINDS:
        return None
    pid = placement["id"] + ":marker"
    source = {"placement_index": placement["index"], "kind": kind, "record_size": len(raw)}
    yaw = placement["word_6"]
    common = {"source": source, "raw": raw}
    if kind == 13 and len(raw) >= 15:
        u16 = lambda o: struct.unpack_from("<H", raw, o)[0]  # noqa: E731
        fields = {
            "spawn_mode": field(raw[1], "u8", 1, "proven", "stored at object +0x24 (N64 o->b40 layout)"),
            "unit_parameter": field(raw[2], "u8", 2, "proven", "stored at object +0x25"),
            "owner_slot": field(raw[3], "u8", 3, "proven", "index into the slot table 0x800D3018 (+0x34)"),
            "spawn_budget": field(u16(4), "u16", 4, "proven", "stored at object +0x14", sentinel={0: "disabled", 0xFFFF: "unlimited"}),
            "unit_type_mask": field(u16(6), "u16", 6, "corroborated", "stored at object +0x18; per-bit use traced on N64 only", bits=15),
            "spawn_interval": field(struct.unpack_from("<I", raw, 8)[0], "u32", 8, "proven", "stored doubled at object +0x20"),
            "max_spawned": field(raw[14], "u8", 14, "proven", "stored at object +0x26 (0 means 255)", sentinel={0: "255"}),
        }
        return marker(pid, PLATFORM, "ps1_kind_13", "unit_spawner", placement["translation"], yaw, fields,
                      evidence=["0x80025928 -> func_80034CD4 (object class 14, 0x34 bytes)",
                                "u16 +0xC (the N64 spawner model) is not read on PS1"], confidence="proven", **common)
    if kind == 27:
        return marker(pid, PLATFORM, "ps1_kind_27", "hazard", placement["translation"], yaw, {},
                      evidence=["0x80025B9C -> func_80027F38(position, 0, 0x7F): no record byte is read; size index is always 0"],
                      confidence="proven", **common)
    if kind == 30 and len(raw) >= 14:
        s16 = lambda o: struct.unpack_from("<h", raw, o)[0]  # noqa: E731
        fields = {name: field(s16(offset), "s16", offset, "proven" if offset in (2, 6, 8, 12) else "unresolved",
                              ("extent read by 0x80025BD0 and scaled by 64/72" if offset in (2, 6, 8, 12) else "not read by the mode-0 path"))
                  for name, offset in (("x0", 2), ("y0", 4), ("z0", 6), ("x1", 8), ("y1", 10), ("z1", 12))}
        fields["zone_mode"] = field(raw[1], "u8", 1, "proven", "selects one of five handlers (table 0x800BD754); 0 writes the lane bounds",
                                    legal=(0, 4))
        return marker(pid, PLATFORM, "ps1_kind_30", "lane_bounds" if raw[1] == 0 else "zone", placement["translation"], yaw, fields,
                      evidence=["table 0x800BD6A4[30] -> inline 0x80025BD0", "same field offsets as N64 Global Assault kind 30"],
                      confidence="proven" if raw[1] == 0 else "unresolved", **common)
    if kind in NO_OP_KINDS:
        return marker(pid, PLATFORM, f"ps1_kind_{kind}", "ignored_by_dispatcher", placement["translation"], yaw, {},
                      evidence=[f"table 0x800BD6A4[{kind}] jumps to the loop tail 0x80026168"], confidence="proven", **common)
    handler = HANDLERS.get(kind, "unknown")
    return marker(pid, PLATFORM, f"ps1_kind_{kind}", "untraced_handler", placement["translation"], yaw, {},
                  evidence=[f"table 0x800BD6A4[{kind}] -> {handler}; record reads untraced",
                            "kind numbers match N64 Global Assault where both are traced (12: model at +4; 13: spawner layout)"],
                  confidence="unresolved", **common)
