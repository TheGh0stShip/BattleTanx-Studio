"""Platform-neutral triangle meshes shared by every adapter (neutral schema v2).

A mesh is what the editor draws, whatever the source platform:

    positions  [[x, y, z]]                    model units, +Y up, right-handed
    materials  [{id, texture, double_sided, alpha_mode, ...}]
    triangles  [{material, corners: [{p, uv, colour, normal}] x3, source}]
               counter-clockwise front faces; uv in texture space (0..1 =
               one texture repeat); colour RGBA 0..1; normal unit vector
    lods       [{index, first_triangle, triangle_count, switch_distance}]
    rejected_triangles  [{source, reason}] triangles the source holds but a
               mesh cannot represent (for example degenerate ones)

Textures are separate records keyed by ID: {id, width, height, format,
wrap: {s, t}, alpha, source, rgba (bytes, never serialised)}.

Source-specific facts (commands, raw words, display lists) stay in the
model record next to the mesh; the mesh never invents data the source
does not provide: a corner without a UV, colour or normal carries None.
"""

from __future__ import annotations

import math

MESH_SCHEMA_VERSION = 2
WRAP_MODES = ("repeat", "mirror", "clamp")
ALPHA_MODES = ("OPAQUE", "MASK", "BLEND")


class MeshError(ValueError):
    pass


class MeshBuilder:
    def __init__(self):
        self.positions: list[list[float]] = []
        self.materials: list[dict] = []
        self.material_keys: dict[str, int] = {}
        self.triangles: list[dict] = []
        self.lods: list[dict] = []
        self.rejected: list[dict] = []

    def reject(self, source: dict, reason: str) -> None:
        self.rejected.append({"source": source, "reason": reason})

    def position(self, xyz) -> int:
        values = [float(component) for component in xyz]
        if len(values) != 3 or not all(math.isfinite(v) for v in values):
            raise MeshError("positions must be three finite numbers")
        self.positions.append(values)
        return len(self.positions) - 1

    def material(self, material: dict) -> int:
        for key in ("id", "double_sided", "alpha_mode"):
            if key not in material:
                raise MeshError(f"material lacks {key}")
        if material["alpha_mode"] not in ALPHA_MODES:
            raise MeshError(f"bad alpha mode {material['alpha_mode']!r}")
        key = material["id"]
        if key not in self.material_keys:
            self.material_keys[key] = len(self.materials)
            self.materials.append(material)
        return self.material_keys[key]

    def triangle(self, material: int, corners: list[dict], source: dict | None = None) -> None:
        if not 0 <= material < len(self.materials):
            raise MeshError("triangle references an unknown material")
        if len(corners) != 3:
            raise MeshError("a triangle needs three corners")
        clean = []
        for corner in corners:
            index = corner["p"]
            if not 0 <= index < len(self.positions):
                raise MeshError("triangle corner references an unknown position")
            for key, size in (("uv", 2), ("colour", 4), ("normal", 3)):
                value = corner.get(key)
                if value is not None and (len(value) != size or not all(math.isfinite(v) for v in value)):
                    raise MeshError(f"corner {key} must be {size} finite numbers")
            clean.append({"p": index, "uv": corner.get("uv"), "colour": corner.get("colour"), "normal": corner.get("normal")})
        if len({corner["p"] for corner in clean}) != 3:
            raise MeshError("degenerate triangle")
        self.triangles.append({"material": material, "corners": clean, "source": source or {}})

    def begin_lod(self, index: int, switch_distance: float | None) -> None:
        self.lods.append({"index": index, "first_triangle": len(self.triangles), "triangle_count": 0, "switch_distance": switch_distance})

    def end_lod(self) -> None:
        self.lods[-1]["triangle_count"] = len(self.triangles) - self.lods[-1]["first_triangle"]

    def build(self) -> dict:
        return {
            "schema_version": MESH_SCHEMA_VERSION,
            "positions": self.positions,
            "materials": self.materials,
            "triangles": self.triangles,
            "lods": self.lods,
            "rejected_triangles": self.rejected,
        }


def bounds(mesh: dict) -> dict:
    if not mesh["positions"]:
        return {"min": [0.0, 0.0, 0.0], "max": [0.0, 0.0, 0.0]}
    return {
        "min": [min(p[axis] for p in mesh["positions"]) for axis in range(3)],
        "max": [max(p[axis] for p in mesh["positions"]) for axis in range(3)],
    }


def ps1_mesh(record: dict, slot_textures: dict[int, dict | None]) -> dict:
    """Convert a neutral PS1 model record into the shared mesh form.

    Uses the same proven rules as the PS1 glTF path: quads split on the
    c1-c3 diagonal, cull flag 2 reversed, colour / 128 (PS1 GPU unity),
    UV texel / size.  Primitives with missing state are not converted.
    """

    from .neutral import triangle_corner_order

    builder = MeshBuilder()
    for vertex in record["vertices"]:
        builder.position(vertex)
    for primitive in record["primitives"]:
        if primitive["missing"]:
            builder.reject({"primitive": primitive["index"]}, "missing state: " + ", ".join(primitive["missing"]))
            continue
        texture = slot_textures.get(primitive["texture_slot"]) if primitive["textured"] else None
        double = primitive["cull"] == "double_sided"
        material = builder.material(
            {
                "id": f"{texture['id'] if texture else 'untextured'}|{'double' if double else 'single'}",
                "texture": texture["id"] if texture else None,
                "double_sided": double,
                "alpha_mode": "MASK" if texture and texture.get("alpha") == "mask" else "OPAQUE",
                "semi_transparent": False,
                "vertex_colour": "pre-lighting material colour, 128 = 1.0",
            }
        )
        corners = primitive["corners"]
        for triangle in triangle_corner_order(primitive):
            out = []
            for corner_index in triangle:
                corner = corners[corner_index]
                rgb = corner["rgb"] if primitive["gouraud"] else corners[0]["rgb"]
                normal = corner["normal"]
                length = math.sqrt(sum(v * v for v in normal)) if normal else 0
                out.append(
                    {
                        "p": corner["vertex"],
                        "uv": [corner["uv"][0] / texture["width"], corner["uv"][1] / texture["height"]] if texture else None,
                        "colour": [min(1.0, ((rgb >> shift) & 0xFF) / 128.0) for shift in (0, 8, 16)] + [1.0],
                        "normal": [v / length for v in normal] if length else None,
                    }
                )
            source = {"primitive": primitive["index"], "command": primitive["command"]}
            try:
                builder.triangle(material, out, source)
            except MeshError as error:
                builder.reject(source, str(error))
    return builder.build()


# -- N64 lighting --------------------------------------------------------------------------
# Some N64 models (Global Assault's tanks among them) are drawn with lighting switched on by the
# code that calls their display list, not by the list itself.  Decoded alone, their vertex "colours"
# are then lighting normals (signed bytes, length 127), which would show as red/green/blue patches.
GAME_LIGHT = (0.37, 0.80, 0.47)  # direction towards the light: high and to one side, like a sunlit level
GAME_AMBIENT, GAME_DIFFUSE = 0.42, 0.68


def normal_coloured_materials(mesh: dict) -> set[int]:
    """Indices of N64 materials decoded as unlit whose every vertex colour is really a unit normal."""

    candidates = {index for index, material in enumerate(mesh["materials"])
                  if "|lit0" in str(material.get("id", "")) and str(material.get("vertex_colour", "")).startswith("shade")}
    if not candidates:
        return set()
    seen: dict[int, list[int]] = {index: [0, 0] for index in candidates}
    for triangle in mesh["triangles"]:
        counts = seen.get(triangle["material"])
        if counts is None:
            continue
        for corner in triangle["corners"]:
            colour = corner.get("colour")
            counts[1] += 1
            if colour:
                signed = [round(v * 255) - 256 if round(v * 255) > 127 else round(v * 255) for v in colour[:3]]
                if abs(math.sqrt(sum(v * v for v in signed)) - 127) < 12:
                    counts[0] += 1
    return {index for index, (normal, total) in seen.items() if total and normal == total}


def shade_from_normal(colour) -> list[float]:
    """Grey light level for a normal stored as a colour, with the vertex alpha kept."""

    signed = [round(v * 255) - 256 if round(v * 255) > 127 else round(v * 255) for v in colour[:3]]
    length = math.sqrt(sum(v * v for v in signed)) or 1.0
    light = math.sqrt(sum(v * v for v in GAME_LIGHT))
    facing = max(0.0, sum(n / length * l / light for n, l in zip(signed, GAME_LIGHT)))
    level = min(1.0, GAME_AMBIENT + GAME_DIFFUSE * facing)
    return [level, level, level, colour[3] if len(colour) > 3 else 1.0]


def with_game_lighting(mesh: dict) -> dict:
    """``mesh`` with normal-as-colour corners replaced by lit grey levels (the input is not changed)."""

    lit = normal_coloured_materials(mesh)
    if not lit:
        return mesh
    triangles = []
    for triangle in mesh["triangles"]:
        if triangle["material"] in lit:
            triangle = {**triangle, "corners": [{**corner, "colour": shade_from_normal(corner["colour"])} for corner in triangle["corners"]]}
        triangles.append(triangle)
    return {**mesh, "triangles": triangles}
