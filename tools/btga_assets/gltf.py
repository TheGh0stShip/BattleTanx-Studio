"""Deterministic glTF 2.0 binary (GLB) writer for neutral model and scene records.

Vertices are not welded: every triangle corner becomes its own vertex, so
per-corner colours, normals and UVs stay exactly as decoded.  Output depends
only on the input records (sorted keys, fixed float packing, no
timestamps), so two runs produce byte-identical files.
"""

from __future__ import annotations

import json
import math
import struct

from .neutral import NeutralError, triangle_corner_order

GLB_MAGIC = 0x46546C67
JSON_CHUNK = 0x4E4F534A
BIN_CHUNK = 0x004E4942
FLOAT = 5126
UNSIGNED_INT = 5125
ARRAY_BUFFER = 34962
ELEMENT_ARRAY_BUFFER = 34963
NEAREST = 9728
CLAMP_TO_EDGE = 33071

# The PS1 GPU modulates texels by vertex colour / 128 (0x80 is 1.0).
COLOUR_UNITY = 128.0


class GltfError(NeutralError):
    pass


class _Builder:
    def __init__(self):
        self.buffer = bytearray()
        self.gltf = {
            "asset": {"version": "2.0", "generator": "btga_assets.gltf"},
            "buffers": [],
            "bufferViews": [],
            "accessors": [],
            "meshes": [],
            "materials": [],
            "nodes": [],
            "scenes": [],
            "scene": 0,
        }
        self.textures: dict[str, int] = {}
        self.materials: dict[tuple, int] = {}

    def _view(self, payload: bytes, target: int | None) -> int:
        while len(self.buffer) % 4:
            self.buffer.append(0)
        view = {"buffer": 0, "byteOffset": len(self.buffer), "byteLength": len(payload)}
        if target is not None:
            view["target"] = target
        self.buffer.extend(payload)
        self.gltf["bufferViews"].append(view)
        return len(self.gltf["bufferViews"]) - 1

    def accessor(self, values: list, kind: str, *, indices: bool = False, bounds: bool = False) -> int:
        width = {"SCALAR": 1, "VEC2": 2, "VEC3": 3}[kind]
        if indices:
            payload = struct.pack(f"<{len(values)}I", *values)
            component = UNSIGNED_INT
            count = len(values)
        else:
            flat = [component for value in values for component in value]
            payload = struct.pack(f"<{len(flat)}f", *flat)
            component = FLOAT
            count = len(values)
        view = self._view(payload, ELEMENT_ARRAY_BUFFER if indices else ARRAY_BUFFER)
        accessor = {"bufferView": view, "componentType": component, "count": count, "type": kind}
        if bounds:
            packed = [struct.unpack("<f", struct.pack("<f", v))[0] for value in values for v in value]
            accessor["min"] = [min(packed[axis::width]) for axis in range(width)]
            accessor["max"] = [max(packed[axis::width]) for axis in range(width)]
        self.gltf["accessors"].append(accessor)
        return len(self.gltf["accessors"]) - 1

    def texture(self, key: str, png: bytes, name: str) -> int:
        if key in self.textures:
            return self.textures[key]
        view = self._view(png, None)
        self.gltf.setdefault("images", []).append({"bufferView": view, "mimeType": "image/png", "name": name})
        self.gltf.setdefault("samplers", [{"magFilter": NEAREST, "minFilter": NEAREST, "wrapS": CLAMP_TO_EDGE, "wrapT": CLAMP_TO_EDGE}])
        self.gltf.setdefault("textures", []).append({"sampler": 0, "source": len(self.gltf["images"]) - 1})
        self.textures[key] = len(self.gltf["textures"]) - 1
        return self.textures[key]

    def material(self, texture: dict | None, double_sided: bool) -> int:
        key = (texture["key"] if texture else None, double_sided)
        if key in self.materials:
            return self.materials[key]
        material = {
            "name": (texture["name"] if texture else "untextured") + (" (double-sided)" if double_sided else ""),
            "doubleSided": double_sided,
            "pbrMetallicRoughness": {"metallicFactor": 0.0, "roughnessFactor": 1.0},
            "extras": {
                "semi_transparent": False,
                "semi_transparency_evidence": "proven: model emitters 0x8007CAE8-0x8007CD7C write GPU codes without bit 1",
            },
        }
        if texture:
            index = self.texture(texture["key"], texture["png"], texture["name"])
            material["pbrMetallicRoughness"]["baseColorTexture"] = {"index": index}
            if texture.get("has_transparent_texels"):
                material["alphaMode"] = "MASK"
                material["alphaCutoff"] = 0.5
            material["extras"]["texture"] = texture["metadata"]
        self.gltf["materials"].append(material)
        self.materials[key] = len(self.gltf["materials"]) - 1
        return self.materials[key]

    def finish(self) -> bytes:
        for key in ("images", "samplers", "textures", "materials"):
            if key in self.gltf and not self.gltf[key]:
                del self.gltf[key]
        while len(self.buffer) % 4:
            self.buffer.append(0)
        self.gltf["buffers"] = [{"byteLength": len(self.buffer)}]
        text = json.dumps(self.gltf, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
        while len(text) % 4:
            text += b" "
        total = 12 + 8 + len(text) + 8 + len(self.buffer)
        return (
            struct.pack("<3I", GLB_MAGIC, 2, total)
            + struct.pack("<2I", len(text), JSON_CHUNK)
            + text
            + struct.pack("<2I", len(self.buffer), BIN_CHUNK)
            + bytes(self.buffer)
        )


def _normalised(normal: list[int]) -> list[float] | None:
    length = math.sqrt(sum(value * value for value in normal))
    if length == 0:
        return None
    return [value / length for value in normal]


def add_model_mesh(builder: _Builder, model: dict, textures: dict[int, dict | None]) -> int | None:
    """Add one neutral PS1 model as a mesh; returns the mesh index.

    `textures` maps texture slot -> texture dict (key, name, png, width,
    height, has_transparent_texels, metadata) or None when unresolved.
    Primitives whose texture cannot be resolved are exported untextured and
    listed in the mesh extras rather than dropped.
    """

    groups: dict[tuple, dict] = {}
    unresolved = []
    vertices = model["vertices"]
    for primitive in model["primitives"]:
        if primitive["missing"]:
            unresolved.append({"primitive": primitive["index"], "missing": primitive["missing"]})
            continue
        texture = None
        if primitive["textured"]:
            texture = textures.get(primitive["texture_slot"])
            if texture is None:
                unresolved.append({"primitive": primitive["index"], "missing": ["texture"]})
        double = primitive["cull"] == "double_sided"
        group = groups.setdefault(
            (texture["key"] if texture else "", double),
            {"texture": texture, "double": double, "positions": [], "colours": [], "normals": [], "uvs": [], "primitives": []},
        )
        group["primitives"].append(primitive["index"])
        corners = primitive["corners"]
        for triangle in triangle_corner_order(primitive):
            for corner_index in triangle:
                corner = corners[corner_index]
                group["positions"].append([float(value) for value in vertices[corner["vertex"]]])
                rgb = corner["rgb"] if primitive["gouraud"] else corners[0]["rgb"]
                group["colours"].append(
                    [min(1.0, ((rgb >> shift) & 0xFF) / COLOUR_UNITY) for shift in (0, 8, 16)]
                )
                group["normals"].append(_normalised(corner["normal"]))
                if texture is not None:
                    u, v = corner["uv"]
                    group["uvs"].append([u / texture["width"], v / texture["height"]])
    if not groups:
        return None
    primitives = []
    for key in sorted(groups, key=lambda item: (item[0], item[1])):
        group = groups[key]
        attributes = {
            "POSITION": builder.accessor(group["positions"], "VEC3", bounds=True),
            "COLOR_0": builder.accessor(group["colours"], "VEC3"),
        }
        if all(normal is not None for normal in group["normals"]):
            attributes["NORMAL"] = builder.accessor(group["normals"], "VEC3")
        if group["texture"] is not None:
            attributes["TEXCOORD_0"] = builder.accessor(group["uvs"], "VEC2")
        indices = builder.accessor(list(range(len(group["positions"]))), "SCALAR", indices=True)
        primitives.append(
            {
                "attributes": attributes,
                "indices": indices,
                "material": builder.material(group["texture"], group["double"]),
                "mode": 4,
                "extras": {"source_primitives": group["primitives"]},
            }
        )
    builder.gltf["meshes"].append(
        {
            "name": model["id"],
            "primitives": primitives,
            "extras": {"source": model["source"], "unresolved_primitives": unresolved},
        }
    )
    return len(builder.gltf["meshes"]) - 1


def model_glb(model: dict, textures: dict[int, dict | None]) -> bytes:
    builder = _Builder()
    mesh = add_model_mesh(builder, model, textures)
    node = {"name": model["id"], "extras": {"source": model["source"]}}
    if mesh is not None:
        node["mesh"] = mesh
    builder.gltf["nodes"].append(node)
    builder.gltf["scenes"].append({"nodes": [0], "extras": {"coordinate_system": model["coordinate_system"]}})
    builder.gltf["asset"]["extras"] = {"source": model["source"], "schema": model["schema"], "schema_version": model["schema_version"]}
    return builder.finish()


def yaw_quaternion(angle_units: int) -> list[float]:
    """Quaternion for the 0x800249A0 matrix [[c,0,s],[0,1,0],[-s,0,c]].

    The angle is a u16 with 65536 units per turn.
    """

    theta = (angle_units & 0xFFFF) * 2.0 * math.pi / 65536.0
    return [0.0, math.sin(theta / 2.0), 0.0, math.cos(theta / 2.0)]


def scene_glb(scene: dict, models: dict[int, dict], textures_for_model: dict[int, dict]) -> bytes:
    """Write a scene: one mesh per referenced model, one node per placement."""

    builder = _Builder()
    meshes: dict[int, int | None] = {}
    for model_index in sorted(models):
        meshes[model_index] = add_model_mesh(builder, models[model_index], textures_for_model.get(model_index, {}))
    roots = []
    for placement in scene["placements"]:
        node = {
            "name": placement["id"],
            "translation": [float(value) for value in placement["translation"]],
            "rotation": yaw_quaternion(placement["word_6"]),
            "extras": {"placement": placement["index"], "kind": placement["kind"]},
        }
        mesh = meshes.get(placement.get("model_index"))
        if mesh is not None:
            node["mesh"] = mesh
        builder.gltf["nodes"].append(node)
        roots.append(len(builder.gltf["nodes"]) - 1)
    builder.gltf["scenes"].append({"name": scene["id"], "nodes": roots, "extras": {"coordinate_system": scene["coordinate_system"]}})
    builder.gltf["asset"]["extras"] = {"scene": scene["id"], "schema": scene["schema"], "schema_version": scene["schema_version"]}
    return builder.finish()


def read_glb(data: bytes) -> tuple[dict, bytes]:
    """Parse a GLB written by this module (used by tests and validators)."""

    if len(data) < 20:
        raise GltfError("GLB shorter than its headers")
    magic, version, total = struct.unpack_from("<3I", data, 0)
    if magic != GLB_MAGIC or version != 2 or total != len(data):
        raise GltfError("bad GLB header")
    json_length, json_type = struct.unpack_from("<2I", data, 12)
    if json_type != JSON_CHUNK or 20 + json_length > len(data):
        raise GltfError("bad GLB JSON chunk")
    document = json.loads(data[20 : 20 + json_length])
    offset = 20 + json_length
    binary = b""
    if offset < len(data):
        bin_length, bin_type = struct.unpack_from("<2I", data, offset)
        if bin_type != BIN_CHUNK or offset + 8 + bin_length > len(data):
            raise GltfError("bad GLB BIN chunk")
        binary = data[offset + 8 : offset + 8 + bin_length]
    return document, binary


# ----------------------------------------------- shared mesh (schema v2) ----

WRAP_CODES = {"repeat": 10497, "mirror": 33648, "clamp": CLAMP_TO_EDGE}


def _texture_png(texture: dict) -> bytes:
    if "png" in texture:
        return texture["png"]
    from .ps1_formats import encode_png_rgba

    return encode_png_rgba(texture["width"], texture["height"], texture["rgba"])


def _mesh_texture(builder: _Builder, texture: dict) -> int:
    key = texture["id"]
    if key in builder.textures:
        return builder.textures[key]
    view = builder._view(_texture_png(texture), None)
    builder.gltf.setdefault("images", []).append({"bufferView": view, "mimeType": "image/png", "name": key})
    wrap = texture.get("wrap") or {"s": "clamp", "t": "clamp"}
    sampler = {"magFilter": NEAREST, "minFilter": NEAREST, "wrapS": WRAP_CODES[wrap["s"]], "wrapT": WRAP_CODES[wrap["t"]]}
    samplers = builder.gltf.setdefault("samplers", [])
    if sampler not in samplers:
        samplers.append(sampler)
    builder.gltf.setdefault("textures", []).append({"sampler": samplers.index(sampler), "source": len(builder.gltf["images"]) - 1})
    builder.textures[key] = len(builder.gltf["textures"]) - 1
    return builder.textures[key]


def _mesh_material(builder: _Builder, material: dict, textures: dict[str, dict]) -> int:
    key = ("mesh", material["id"])
    if key in builder.materials:
        return builder.materials[key]
    out = {
        "name": material["id"],
        "doubleSided": bool(material["double_sided"]),
        "pbrMetallicRoughness": {"metallicFactor": 0.0, "roughnessFactor": 1.0},
        "extras": {k: v for k, v in sorted(material.items()) if k not in ("id",)},
    }
    if material["alpha_mode"] != "OPAQUE":
        out["alphaMode"] = material["alpha_mode"]
        if material["alpha_mode"] == "MASK":
            out["alphaCutoff"] = 0.5
    if material.get("base_colour") is not None:
        out["pbrMetallicRoughness"]["baseColorFactor"] = list(material["base_colour"])
    texture = textures.get(material.get("texture")) if material.get("texture") else None
    if texture is not None and "rgba" in texture or texture is not None and "png" in texture:
        out["pbrMetallicRoughness"]["baseColorTexture"] = {"index": _mesh_texture(builder, texture)}
    builder.gltf["materials"].append(out)
    builder.materials[key] = len(builder.gltf["materials"]) - 1
    return builder.materials[key]


def add_mesh(builder: _Builder, name: str, mesh: dict, textures: dict[str, dict], extras: dict, lod: int | None = 0) -> int | None:
    """Add a shared-schema mesh; with lod set, only that LOD's triangles."""

    triangles = mesh["triangles"]
    if lod is not None and mesh.get("lods"):
        selected = [entry for entry in mesh["lods"] if entry["index"] == lod]
        if selected:
            first = selected[0]["first_triangle"]
            triangles = triangles[first : first + selected[0]["triangle_count"]]
    groups: dict[int, dict] = {}
    for triangle in triangles:
        group = groups.setdefault(triangle["material"], {"positions": [], "uvs": [], "colours": [], "normals": []})
        for corner in triangle["corners"]:
            group["positions"].append(mesh["positions"][corner["p"]])
            group["uvs"].append(corner["uv"])
            group["colours"].append(corner["colour"])
            group["normals"].append(corner["normal"])
    if not groups:
        return None
    primitives = []
    for material_index in sorted(groups):
        group = groups[material_index]
        material = mesh["materials"][material_index]
        attributes = {"POSITION": builder.accessor(group["positions"], "VEC3", bounds=True)}
        if all(colour is not None for colour in group["colours"]):
            attributes["COLOR_0"] = builder.accessor([c[:3] for c in group["colours"]], "VEC3")
        if all(normal is not None for normal in group["normals"]):
            attributes["NORMAL"] = builder.accessor(group["normals"], "VEC3")
        texture = textures.get(material.get("texture")) if material.get("texture") else None
        if texture is not None and all(uv is not None for uv in group["uvs"]):
            attributes["TEXCOORD_0"] = builder.accessor(group["uvs"], "VEC2")
        indices = builder.accessor(list(range(len(group["positions"]))), "SCALAR", indices=True)
        primitives.append({"attributes": attributes, "indices": indices, "material": _mesh_material(builder, material, textures), "mode": 4})
    builder.gltf["meshes"].append({"name": name, "primitives": primitives, "extras": extras})
    return len(builder.gltf["meshes"]) - 1


def mesh_glb(record: dict, textures: dict[str, dict]) -> bytes:
    builder = _Builder()
    mesh = add_mesh(builder, record["id"], record["mesh"], textures, {"source": record["source"]})
    node = {"name": record["id"], "extras": {"source": record["source"]}}
    if mesh is not None:
        node["mesh"] = mesh
    builder.gltf["nodes"].append(node)
    builder.gltf["scenes"].append({"nodes": [0], "extras": {"coordinate_system": record.get("coordinate_system")}})
    builder.gltf["asset"]["extras"] = {"source": record["source"], "schema": record["schema"], "schema_version": record["schema_version"]}
    return builder.finish()


PLANAR_DISPLAY_HEIGHT = 8.0


def _box_mesh(box: dict) -> dict:
    """Box geometry; planar shapes (no stored height) become a thin display slab."""

    (x0, y0, z0), (x1, y1, z1) = box["min"], box["max"]
    if y0 is None or y1 is None:
        y0, y1 = 0.0, PLANAR_DISPLAY_HEIGHT
    corners = [(x, y, z) for x in (x0, x1) for y in (y0, y1) for z in (z0, z1)]
    faces = [(0, 1, 3, 2), (4, 6, 7, 5), (0, 4, 5, 1), (2, 3, 7, 6), (0, 2, 6, 4), (1, 5, 7, 3)]
    triangles = []
    for a, b, c, d in faces:
        for tri in ((a, b, c), (a, c, d)):
            triangles.append({"material": 0, "corners": [{"p": i, "uv": None, "colour": None, "normal": None} for i in tri], "source": {}})
    return {
        "positions": [list(map(float, corner)) for corner in corners],
        "materials": [{"id": box.get("material", "collision"), "double_sided": True, "alpha_mode": "BLEND", "texture": None,
                       "base_colour": box.get("colour", [1.0, 0.2, 0.2, 0.35])}],
        "triangles": triangles,
        "lods": [],
    }


def scene_mesh_glb(scene: dict, models: dict[str, dict], textures: dict[str, dict]) -> bytes:
    """Scene GLB: visible instances, then 'collision' and 'markers' layers."""

    builder = _Builder()
    meshes: dict[str, int | None] = {}
    for model_id in sorted(models):
        meshes[model_id] = add_mesh(builder, model_id, models[model_id]["mesh"], textures, {"source": models[model_id]["source"]})
    roots = []

    def node(entry: dict, mesh: int | None, translation, yaw) -> int:
        item = {"name": entry["id"], "translation": [float(v) for v in translation], "rotation": yaw_quaternion(yaw), "extras": entry.get("extras", {})}
        if mesh is not None:
            item["mesh"] = mesh
        builder.gltf["nodes"].append(item)
        return len(builder.gltf["nodes"]) - 1

    visible = [node(i, meshes.get(i.get("model")), i["translation"], i["yaw"]) for i in scene["instances"]]
    builder.gltf["nodes"].append({"name": "visible", "children": visible})
    roots.append(len(builder.gltf["nodes"]) - 1)
    collision_nodes = []
    for box in scene.get("collision", []):
        info = {"collision": box.get("extras", {}), "shape": box.get("shape"), "vertical": box.get("vertical"), "orientation": box.get("orientation")}
        mesh_index = add_mesh(builder, box["id"], _box_mesh(box), {}, info)
        collision_nodes.append(node(box, mesh_index, box["translation"], box["yaw"]))
    builder.gltf["nodes"].append({"name": "collision", "children": collision_nodes})
    roots.append(len(builder.gltf["nodes"]) - 1)
    marker_nodes = []
    for marker in scene.get("markers", []):
        size = 24.0
        mesh_index = add_mesh(builder, marker["id"], _box_mesh({"min": [-size, 0, -size], "max": [size, 4 * size, size], "material": "marker:" + marker["category"],
                                                                   "colour": marker.get("colour", [0.2, 0.6, 1.0, 0.8])}), {}, {"marker": marker["category"]})
        marker_nodes.append(node(marker, mesh_index, marker["translation"], marker.get("yaw", 0)))
    builder.gltf["nodes"].append({"name": "markers", "children": marker_nodes})
    roots.append(len(builder.gltf["nodes"]) - 1)
    builder.gltf["scenes"].append({"name": scene["id"], "nodes": roots, "extras": {"coordinate_system": scene.get("coordinate_system")}})
    builder.gltf["asset"]["extras"] = {"scene": scene["id"], "schema": scene["schema"], "schema_version": scene["schema_version"]}
    return builder.finish()
