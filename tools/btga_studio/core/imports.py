"""Bring models and textures from outside the games into Studio.

Formats: Wavefront OBJ (with MTL materials), glTF 2.0 (``.gltf`` with
embedded or neighbouring files, and ``.glb``), and pictures (PNG, JPEG, BMP,
TGA).  A picture can stay a texture or become a ready-to-place object: a
ground tile, an upright panel, a box or a sky panorama.

``read_file`` turns one file into an ``ImportResult`` without changing
anything, so the import dialog can preview it.  ``UserLibrary.add`` then
stores the result in the user's library: a normal Studio catalog under the
local data folder whose models and textures carry ``user:`` content IDs.
Projects refer to them by ID; content packages embed them (with the credit
and licence the user entered), and ``UserLibrary.add_package`` restores them
from a package on another computer.

Import never follows absolute paths or paths that leave the model's folder;
a texture that is missing there is looked up by file name next to the model
(a common fix for files exported on another computer), otherwise it is
reported and the material keeps its plain colour.  The library stores only
file names and SHA-256 hashes, never folder paths.
"""

from __future__ import annotations

import base64
import hashlib
import json
import math
import re
import shutil
import struct
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from tools.btga_assets import mesh as meshmod

from . import imaging

PLATFORM = "user"
IMPORTER_VERSION = 1
MODEL_FORMATS = (".obj", ".gltf", ".glb")
IMAGE_FORMATS = (".png", ".jpg", ".jpeg", ".bmp", ".tga")
LIMITS = {"file_bytes": 128 * 1024 * 1024, "triangles": 200_000, "materials": 64, "textures": 64, "library_bytes": 2 * 1024 ** 3}
# Studio units per source unit.  The starter tank is about 110 units long, so one metre is about 16 units.
UNITS = {"metres": 16.0, "centimetres": 0.16, "feet": 4.8768, "inches": 0.4064, "studio units": 1.0}
SHAPES = ("texture", "ground tile", "upright panel", "box", "sky panorama")
GROUPS = re.compile(r"^[^\x00-\x1f/\\:*?\"<>|]{1,40}$")


class ImportProblem(ValueError):
    pass


@dataclass
class ImportOptions:
    units: str = "metres"
    scale: float = 1.0  # extra multiplier on top of the units
    up_axis: str = "y"  # "y" (glTF, most OBJ) or "z" (CAD, SketchUp, some Blender exports)
    ground: bool = True  # stand the model on y = 0, centred on its footprint
    texture_limit: int = 512
    shape: str = "texture"  # what a picture becomes
    size: tuple[float, float, float] = (512.0, 512.0, 512.0)  # width, height, depth of a picture-made object
    repeat: float = 1.0  # texture repeats across a picture-made object
    group: str = "Imported"
    credit: str = ""  # where it is from and its licence; travels with packages
    name: str | None = None


@dataclass
class ImportResult:
    file: str
    sha256: str
    format: str
    models: dict[str, dict] = field(default_factory=dict)  # id -> neutral model record
    textures: dict[str, dict] = field(default_factory=dict)  # id -> texture record (with rgba)
    warnings: list[str] = field(default_factory=list)

    @property
    def triangles(self) -> int:
        return sum(len(m["mesh"]["triangles"]) for m in self.models.values())

    def summary(self) -> str:
        parts = []
        if self.models:
            parts.append(f"{len(self.models)} model{'s' if len(self.models) != 1 else ''} ({self.triangles:,} triangles)")
        if self.textures:
            parts.append(f"{len(self.textures)} texture{'s' if len(self.textures) != 1 else ''}")
        return ", ".join(parts) or "nothing to import"


def content_id(kind: str, payload) -> str:
    text = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return f"user:{kind}:{hashlib.sha256(f'user{IMPORTER_VERSION}|{kind}|{text}'.encode()).hexdigest()[:24]}"


def _label(text: str) -> str:
    cleaned = re.sub(r"[^\w\- ]+", " ", text, flags=re.UNICODE).strip()
    return re.sub(r"\s+", " ", cleaned)[:48] or "imported"


# ------------------------------------------------------------------ textures --

def make_texture(name: str, width: int, height: int, rgba: bytes, options: ImportOptions, wrap: str = "repeat", origin: dict | None = None) -> dict:
    width, height, rgba = imaging.fit(width, height, rgba, options.texture_limit)
    alpha = imaging.alpha_kind(rgba)
    record = {"width": width, "height": height, "rgba": rgba, "wrap": {"s": wrap, "t": wrap}, "alpha": alpha, "format": "rgba8",
              "label": _label(name), "source": {"platform": PLATFORM, "label": _label(name), "library": f"user:{options.group}",
                                                 "import": dict(origin or {}, credit=options.credit)}}
    record["id"] = content_id("texture", {"rgba": hashlib.sha256(rgba).hexdigest(), "width": width, "height": height, "wrap": wrap})
    return record


# -------------------------------------------------------------------- meshes --

class _Mesh:
    """Triangles collected from a source file before conversion: positions in source units."""

    def __init__(self):
        self.triangles: list[tuple] = []  # (material key, ((pos, uv, colour) * 3))
        self.materials: dict[str, dict] = {}  # key -> {"texture": record id | None, "colour": rgba, "double_sided": bool, "alpha": mode}

    def material(self, key: str, texture: dict | None, colour, double_sided: bool = False, alpha: str = "OPAQUE") -> str:
        if key not in self.materials:
            self.materials[key] = {"texture": texture, "colour": tuple(float(c) for c in colour), "double_sided": double_sided, "alpha": alpha}
        return key


def _convert(name: str, source: _Mesh, options: ImportOptions, textures: dict[str, dict], warnings: list[str], origin: dict) -> dict | None:
    if not source.triangles:
        return None
    if len(source.triangles) > LIMITS["triangles"]:
        raise ImportProblem(f"{name} has {len(source.triangles):,} triangles; models up to {LIMITS['triangles']:,} triangles can be imported. "
                            "Simplify it in a 3D program first")
    if len(source.materials) > LIMITS["materials"]:
        raise ImportProblem(f"{name} uses {len(source.materials)} materials; up to {LIMITS['materials']} are supported")
    factor = UNITS[options.units] * float(options.scale)
    if not (math.isfinite(factor) and factor > 0):
        raise ImportProblem("the scale must be a positive number")
    points = np.asarray([corner[0] for _, corners in source.triangles for corner in corners], dtype=np.float64).reshape(-1, 3)
    if not np.all(np.isfinite(points)):
        raise ImportProblem(f"{name} contains invalid coordinates")
    if options.up_axis == "z":
        points = np.stack([points[:, 0], points[:, 2], -points[:, 1]], axis=1)
    elif options.up_axis != "y":
        raise ImportProblem("the up axis must be y or z")
    points = points * factor
    if options.ground:
        low, high = points.min(axis=0), points.max(axis=0)
        points = points - np.array([(low[0] + high[0]) / 2, low[1], (low[2] + high[2]) / 2])
    extent = float(np.abs(points).max(initial=0.0))
    if extent > 60_000:
        raise ImportProblem(f"{name} would be {extent:,.0f} units across at this scale; choose smaller units or a smaller scale")
    if extent < 1:
        warnings.append(f"{name} is smaller than one unit at this scale; choose larger units")
    builder = meshmod.MeshBuilder()
    index: dict[tuple, int] = {}
    materials = {}
    for key, spec in sorted(source.materials.items()):
        material = {"id": f"{spec['texture'] or 'untextured'}|{key}|user", "double_sided": bool(spec["double_sided"]), "alpha_mode": spec["alpha"],
                    "texture": spec["texture"]}
        materials[key] = builder.material(material)
    skipped = 0
    for number, (key, corners) in enumerate(source.triangles):
        spec = source.materials[key]
        out = []
        for offset, (_, uv, colour) in enumerate(corners):
            position = tuple(round(float(v), 4) for v in points[number * 3 + offset])
            if position not in index:
                index[position] = builder.position(position)
            tint = tuple(a * b for a, b in zip(colour or (1.0, 1.0, 1.0, 1.0), spec["colour"]))
            out.append({"p": index[position], "uv": [round(float(uv[0]), 5), round(float(uv[1]), 5)] if uv is not None and spec["texture"] else None,
                        "colour": None if tint == (1.0, 1.0, 1.0, 1.0) else [round(c, 4) for c in tint], "normal": None})
        if len({c["p"] for c in out}) != 3:
            skipped += 1
            continue
        builder.triangle(materials[key], out)
    if skipped:
        warnings.append(f"{name}: {skipped} zero-area triangle(s) were left out")
    mesh = builder.build()
    if not mesh["triangles"]:
        return None
    label = _label(name)
    record = {"schema": "btga-neutral", "schema_version": 1, "kind": "model",
              "source": {"platform": PLATFORM, "label": label, "library": f"user:{options.group}", "import": dict(origin, credit=options.credit)},
              "mesh": mesh, "bounds": meshmod.bounds(mesh)}
    record["id"] = content_id("model", {"label": label, "mesh": mesh})
    used = {m.get("texture") for m in mesh["materials"] if m.get("texture")}
    missing = used - set(textures)
    if missing:
        raise ImportProblem(f"internal error: textures {sorted(missing)} were not decoded")
    return record


# ----------------------------------------------------------------------- OBJ --

def _local_file(folder: Path, reference: str, warnings: list[str], what: str) -> Path | None:
    """A file referenced by a model: inside the model's folder, else the same name next to the model."""

    reference = reference.strip().strip('"').replace("\\", "/")
    if not reference or reference.startswith("data:"):
        return None
    candidate = Path(reference)
    folder = folder.resolve()
    if not candidate.is_absolute() and ":" not in reference.split("/")[0]:
        resolved = (folder / candidate).resolve()
        if resolved.is_file() and folder in resolved.parents:
            return resolved
    fallback = folder / candidate.name
    if candidate.name and fallback.is_file():
        return fallback
    warnings.append(f"{what} {candidate.name or reference!r} was not found next to the model; that material keeps its plain colour")
    return None


def _image_texture(path: Path, options: ImportOptions, textures: dict, cache: dict, warnings: list[str]) -> str | None:
    if path in cache:
        return cache[path]
    try:
        data = path.read_bytes()
        if len(data) > LIMITS["file_bytes"]:
            raise imaging.ImageError("the picture is too large")
        width, height, rgba = imaging.decode(data, path.name)
    except (OSError, imaging.ImageError) as error:
        warnings.append(f"texture {path.name}: {error}")
        cache[path] = None
        return None
    record = make_texture(path.stem, width, height, rgba, options, origin={"file": path.name, "sha256": hashlib.sha256(data).hexdigest()})
    textures[record["id"]] = record
    cache[path] = record["id"]
    return record["id"]


def _mtl(path: Path, options: ImportOptions, textures: dict, cache: dict, warnings: list[str]) -> dict[str, dict]:
    out: dict[str, dict] = {}
    current = None
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        parts = raw.strip().split(None, 1)
        if not parts or parts[0].startswith("#"):
            continue
        keyword, rest = parts[0], parts[1] if len(parts) > 1 else ""
        if keyword == "newmtl":
            current = out.setdefault(rest.strip(), {"colour": [1.0, 1.0, 1.0, 1.0], "texture": None})
        elif current is None:
            continue
        elif keyword == "Kd":
            values = [float(v) for v in rest.split()[:3]]
            if len(values) == 3:
                current["colour"][:3] = values
        elif keyword in ("d", "Tr"):
            value = float(rest.split()[0])
            current["colour"][3] = value if keyword == "d" else 1.0 - value
        elif keyword == "map_Kd":
            reference = rest.split()[-1] if rest.split() else ""  # options such as -s or -o come first
            found = _local_file(path.parent, reference, warnings, "texture")
            if found is not None:
                current["texture"] = _image_texture(found, options, textures, cache, warnings)
    return out


def read_obj(path: Path, options: ImportOptions) -> ImportResult:
    data = Path(path).read_bytes()
    result = ImportResult(Path(path).name, hashlib.sha256(data).hexdigest(), "obj")
    positions, uvs, colours = [], [], []
    objects: dict[str, _Mesh] = {}
    materials: dict[str, dict] = {}
    cache: dict = {}
    current_material = "default"
    name = options.name or Path(path).stem
    current_object = name
    for number, raw in enumerate(data.decode("utf-8", errors="replace").splitlines(), 1):
        parts = raw.split()
        if not parts or parts[0].startswith("#"):
            continue
        keyword = parts[0]
        try:
            if keyword == "v":
                positions.append(tuple(float(v) for v in parts[1:4]))
                colours.append(tuple(float(v) for v in parts[4:7]) + (1.0,) if len(parts) >= 7 else None)
            elif keyword == "vt":
                uvs.append((float(parts[1]), 1.0 - float(parts[2]) if len(parts) > 2 else 1.0))  # OBJ v runs upwards
            elif keyword == "mtllib":
                for reference in raw.split(None, 1)[1].split():
                    found = _local_file(Path(path).parent, reference, result.warnings, "material file")
                    if found is not None:
                        materials.update(_mtl(found, options, result.textures, cache, result.warnings))
            elif keyword == "usemtl":
                current_material = raw.split(None, 1)[1].strip() if len(parts) > 1 else "default"
            elif keyword == "f":
                corners = []
                for token in parts[1:]:
                    fields = token.split("/")
                    p = int(fields[0])
                    p = p - 1 if p > 0 else len(positions) + p
                    t = None
                    if len(fields) > 1 and fields[1]:
                        t = int(fields[1])
                        t = t - 1 if t > 0 else len(uvs) + t
                    if not 0 <= p < len(positions) or (t is not None and not 0 <= t < len(uvs)):
                        raise ImportProblem(f"line {number} refers to a vertex that does not exist")
                    corners.append((positions[p], uvs[t] if t is not None else None, colours[p]))
                if len(corners) < 3:
                    continue
                mesh = objects.setdefault(current_object, _Mesh())
                spec = materials.get(current_material, {"colour": [0.8, 0.8, 0.8, 1.0], "texture": None})
                alpha = "BLEND" if spec["colour"][3] < 1.0 else "MASK" if spec["texture"] and \
                    result.textures[spec["texture"]]["alpha"] == "mask" else "BLEND" if spec["texture"] and \
                    result.textures[spec["texture"]]["alpha"] == "blend" else "OPAQUE"
                key = mesh.material(current_material, spec["texture"], spec["colour"], alpha=alpha)
                for i in range(1, len(corners) - 1):  # fan triangulation of convex polygons
                    mesh.triangles.append((key, (corners[0], corners[i], corners[i + 1])))
        except (ValueError, IndexError) as error:
            raise ImportProblem(f"{Path(path).name} line {number} could not be read ({error})") from error
    for object_name, mesh in sorted(objects.items()):
        record = _convert(object_name, mesh, options, result.textures, result.warnings, {"file": result.file, "sha256": result.sha256, "format": "obj"})
        if record is not None:
            result.models[record["id"]] = record
    _keep_used(result)
    return result


def _keep_used(result: ImportResult) -> None:
    """Drop textures no model uses (for example an unused MTL entry)."""

    used = {m.get("texture") for record in result.models.values() for m in record["mesh"]["materials"]}
    result.textures = {k: v for k, v in result.textures.items() if k in used}


# ---------------------------------------------------------------------- glTF --

COMPONENTS = {5120: ("b", 1), 5121: ("B", 1), 5122: ("h", 2), 5123: ("H", 2), 5125: ("I", 4), 5126: ("f", 4)}
WIDTHS = {"SCALAR": 1, "VEC2": 2, "VEC3": 3, "VEC4": 4, "MAT4": 16}


def _matrix(node: dict) -> np.ndarray:
    if "matrix" in node:
        return np.asarray(node["matrix"], dtype=np.float64).reshape(4, 4).T
    x, y, z, w = node.get("rotation", (0.0, 0.0, 0.0, 1.0))
    rotation = np.array([[1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)],
                         [2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)],
                         [2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)]])
    out = np.eye(4)
    out[:3, :3] = rotation * np.asarray(node.get("scale", (1.0, 1.0, 1.0)))
    out[:3, 3] = node.get("translation", (0.0, 0.0, 0.0))
    return out


def read_gltf(path: Path, options: ImportOptions) -> ImportResult:
    path = Path(path)
    data = path.read_bytes()
    result = ImportResult(path.name, hashlib.sha256(data).hexdigest(), "gltf")
    binary = b""
    if data[:4] == b"glTF":
        version, total = struct.unpack_from("<II", data, 4)
        if version != 2:
            raise ImportProblem("only glTF 2.0 files are supported")
        position, document = 12, None
        while position + 8 <= min(total, len(data)):
            length, kind = struct.unpack_from("<II", data, position)
            chunk = data[position + 8:position + 8 + length]
            if kind == 0x4E4F534A:
                document = json.loads(chunk.decode("utf-8"))
            elif kind == 0x004E4942:
                binary = chunk
            position += 8 + length + ((-length) % 4)
        if document is None:
            raise ImportProblem(f"{path.name} has no glTF description")
    else:
        try:
            document = json.loads(data.decode("utf-8"))
        except ValueError as error:
            raise ImportProblem(f"{path.name} is not a glTF file ({error})") from error
    if not str((document.get("asset") or {}).get("version", "")).startswith("2"):
        raise ImportProblem("only glTF 2.0 files are supported")
    unsupported = set(document.get("extensionsRequired", [])) - {"KHR_materials_unlit", "KHR_texture_transform"}
    if unsupported:
        raise ImportProblem(f"{path.name} needs glTF extensions Studio cannot read: {', '.join(sorted(unsupported))}")
    buffers = []
    for number, buffer in enumerate(document.get("buffers", [])):
        uri = buffer.get("uri")
        if uri is None:
            buffers.append(binary)
        elif uri.startswith("data:"):
            buffers.append(base64.b64decode(uri.split(",", 1)[1]))
        else:
            found = _local_file(path.parent, _unquote(uri), result.warnings, "buffer")
            if found is None:
                raise ImportProblem(f"{path.name} needs the file {Path(_unquote(uri)).name}; keep it next to the model")
            buffers.append(found.read_bytes())

    def view_bytes(index: int) -> bytes:
        view = document["bufferViews"][index]
        buffer = buffers[view["buffer"]]
        start = view.get("byteOffset", 0)
        chunk = buffer[start:start + view["byteLength"]]
        if len(chunk) != view["byteLength"]:
            raise ImportProblem(f"{path.name} refers to data beyond the end of a buffer")
        return chunk

    def accessor(index: int) -> np.ndarray:
        spec = document["accessors"][index]
        code, size = COMPONENTS[spec["componentType"]]
        width = WIDTHS[spec["type"]]
        count = spec["count"]
        if "bufferView" not in spec:
            return np.zeros((count, width))
        view = document["bufferViews"][spec["bufferView"]]
        raw = view_bytes(spec["bufferView"])
        stride = view.get("byteStride") or size * width
        start = spec.get("byteOffset", 0)
        if count and start + stride * (count - 1) + size * width > len(raw):
            raise ImportProblem(f"{path.name} has an accessor that runs past its data")
        dtype = np.dtype("<" + code)
        values = np.lib.stride_tricks.as_strided(np.frombuffer(raw, dtype=np.uint8, offset=start, count=len(raw) - start),
                                                 shape=(count, width * size), strides=(stride, 1)).copy()
        out = values.view(dtype).reshape(count, width).astype(np.float64)
        if spec.get("normalized"):
            out /= float(np.iinfo(dtype).max)
        if "sparse" in spec:
            raise ImportProblem(f"{path.name} uses sparse accessors, which are not supported; re-export it without them")
        return out

    images: dict[int, str | None] = {}

    def image_texture(texture_index: int | None, wrap: str) -> str | None:
        if texture_index is None:
            return None
        texture = document["textures"][texture_index]
        source = texture.get("source")
        if source is None:
            result.warnings.append(f"{path.name}: a texture uses an image format Studio cannot read; save images as PNG or JPEG")
            return None
        if source in images:
            return images[source]
        image = document["images"][source]
        label = image.get("name") or f"{path.stem} image {source}"
        try:
            if "bufferView" in image:
                payload = view_bytes(image["bufferView"])
            elif image.get("uri", "").startswith("data:"):
                payload = base64.b64decode(image["uri"].split(",", 1)[1])
            else:
                found = _local_file(path.parent, _unquote(image.get("uri", "")), result.warnings, "texture")
                payload = found.read_bytes() if found else None
                label = image.get("name") or (found.stem if found else label)
            if payload is None:
                images[source] = None
                return None
            width, height, rgba = imaging.decode(payload, label)
        except imaging.ImageError as error:
            result.warnings.append(f"texture {label}: {error}")
            images[source] = None
            return None
        record = make_texture(label, width, height, rgba, options, wrap,
                              origin={"file": result.file, "sha256": result.sha256, "image": source})
        result.textures[record["id"]] = record
        images[source] = record["id"]
        return record["id"]

    mesh_out = _Mesh()

    def add_primitive(primitive: dict, transform: np.ndarray) -> None:
        mode = primitive.get("mode", 4)
        if mode != 4:
            result.warnings.append(f"{path.name}: a part drawn as {'points' if mode == 0 else 'lines' if mode < 4 else 'strips or fans'} was left out")
            return
        attributes = primitive["attributes"]
        if "POSITION" not in attributes:
            return
        points = accessor(attributes["POSITION"])
        points = (np.c_[points, np.ones(len(points))] @ transform.T)[:, :3]
        uv = accessor(attributes["TEXCOORD_0"]) if "TEXCOORD_0" in attributes else None
        colour = accessor(attributes["COLOR_0"]) if "COLOR_0" in attributes else None
        if colour is not None and colour.shape[1] == 3:
            colour = np.c_[colour, np.ones(len(colour))]
        indices = accessor(primitive["indices"]).astype(np.int64).reshape(-1) if "indices" in primitive else np.arange(len(points))
        if len(indices) % 3 or (len(indices) and (indices.min() < 0 or indices.max() >= len(points))):
            raise ImportProblem(f"{path.name} has broken triangle indices")
        material_index = primitive.get("material")
        material = document.get("materials", [])[material_index] if material_index is not None else {}
        pbr = material.get("pbrMetallicRoughness", {})
        sampler_wrap = "repeat"
        texture_info = pbr.get("baseColorTexture")
        if texture_info is not None:
            sampler = document["textures"][texture_info["index"]].get("sampler")
            if sampler is not None and document["samplers"][sampler].get("wrapS") == 33071:
                sampler_wrap = "clamp"
        texture = image_texture(texture_info["index"] if texture_info else None, sampler_wrap)
        alpha = {"MASK": "MASK", "BLEND": "BLEND"}.get(material.get("alphaMode"), "OPAQUE")
        key = mesh_out.material(f"m{material_index}" if material_index is not None else "default", texture,
                                pbr.get("baseColorFactor", (1.0, 1.0, 1.0, 1.0)), bool(material.get("doubleSided")), alpha)
        flip = np.linalg.det(transform[:3, :3]) < 0
        for a, b, c in indices.reshape(-1, 3):
            order = (a, c, b) if flip else (a, b, c)
            mesh_out.triangles.append((key, tuple((tuple(points[i]), tuple(uv[i]) if uv is not None else None,
                                                   tuple(colour[i]) if colour is not None else None) for i in order)))

    def visit(node_index: int, parent: np.ndarray, depth: int) -> None:
        if depth > 64:
            raise ImportProblem(f"{path.name} has a node hierarchy that is too deep or loops")
        node = document["nodes"][node_index]
        transform = parent @ _matrix(node)
        if "mesh" in node:
            for primitive in document["meshes"][node["mesh"]]["primitives"]:
                add_primitive(primitive, transform)
                if len(mesh_out.triangles) > LIMITS["triangles"]:
                    raise ImportProblem(f"{path.name} has more than {LIMITS['triangles']:,} triangles; simplify it first")
        for child in node.get("children", []):
            visit(child, transform, depth + 1)

    try:
        scenes = document.get("scenes") or [{"nodes": list(range(len(document.get("nodes", []))))}]
        roots = scenes[document.get("scene", 0)].get("nodes", [])
        for root in roots:
            visit(root, np.eye(4), 0)
    except (KeyError, IndexError, TypeError) as error:
        raise ImportProblem(f"{path.name} is not a valid glTF file (missing {error})") from error
    if document.get("skins") or document.get("animations"):
        result.warnings.append(f"{path.name}: animation and skinning are not imported; the model keeps its resting pose")
    record = _convert(options.name or path.stem, mesh_out, options, result.textures, result.warnings,
                      {"file": result.file, "sha256": result.sha256, "format": "gltf"})
    if record is not None:
        result.models[record["id"]] = record
    _keep_used(result)
    return result


def _unquote(uri: str) -> str:
    from urllib.parse import unquote

    return unquote(uri)


# ------------------------------------------------------------------ pictures --

def shape_mesh(shape: str, texture: str, size: tuple[float, float, float], repeat: float, double_sided: bool, alpha: str) -> dict:
    """A placeable object showing one texture (shared with the sky panorama)."""

    width, height, depth = (float(v) for v in size)
    w, d = width / 2, depth / 2
    r = float(repeat)
    builder = meshmod.MeshBuilder()
    material = builder.material({"id": f"{texture}|{shape}|user", "double_sided": double_sided, "alpha_mode": alpha, "texture": texture})

    def quad(corners, uvs):
        base = len(builder.positions)
        for corner in corners:
            builder.position(corner)
        for tri in ((0, 1, 2), (0, 2, 3)):
            builder.triangle(material, [{"p": base + i, "uv": list(uvs[i]), "colour": None, "normal": None} for i in tri])

    if shape == "ground tile":
        quad([(-w, 0.5, d), (w, 0.5, d), (w, 0.5, -d), (-w, 0.5, -d)], [(0, r), (r, r), (r, 0), (0, 0)])
    elif shape == "upright panel":
        quad([(-w, 0, 0), (w, 0, 0), (w, height, 0), (-w, height, 0)], [(0, r), (r, r), (r, 0), (0, 0)])
    elif shape == "box":
        x0, x1, z0, z1 = -w, w, -d, d
        side = [(0, r), (r, r), (r, 0), (0, 0)]
        quad([(x0, 0, z1), (x1, 0, z1), (x1, height, z1), (x0, height, z1)], side)
        quad([(x1, 0, z0), (x0, 0, z0), (x0, height, z0), (x1, height, z0)], side)
        quad([(x1, 0, z1), (x1, 0, z0), (x1, height, z0), (x1, height, z1)], side)
        quad([(x0, 0, z0), (x0, 0, z1), (x0, height, z1), (x0, height, z0)], side)
        quad([(x0, height, z1), (x1, height, z1), (x1, height, z0), (x0, height, z0)], side)
    else:
        raise ImportProblem(f"unknown shape {shape!r}")
    return builder.build()


def read_image(path: Path, options: ImportOptions) -> ImportResult:
    path = Path(path)
    data = path.read_bytes()
    result = ImportResult(path.name, hashlib.sha256(data).hexdigest(), "image")
    try:
        width, height, rgba = imaging.decode(data, path.name)
    except imaging.ImageError as error:
        raise ImportProblem(str(error)) from error
    name = options.name or path.stem
    origin = {"file": path.name, "sha256": result.sha256, "format": "image"}
    if options.shape == "sky panorama":
        from . import sky

        record = make_texture(name, width, height, rgba, ImportOptions(**{**options.__dict__, "texture_limit": 4096}),
                              "clamp", origin)
        record["sky"] = sky.panorama_info(width, height)
        result.textures[record["id"]] = record
        return result
    record = make_texture(name, width, height, rgba, options, origin=origin)
    result.textures[record["id"]] = record
    if options.shape == "texture":
        return result
    if any(not (math.isfinite(v) and 1 <= v <= 20_000) for v in options.size):
        raise ImportProblem("object sizes must be between 1 and 20,000 units")
    alpha = {"opaque": "OPAQUE", "mask": "MASK"}.get(record["alpha"], "BLEND")
    mesh = shape_mesh(options.shape, record["id"], options.size, options.repeat, options.shape != "box", alpha)
    label = _label(f"{name} {options.shape}")
    model = {"schema": "btga-neutral", "schema_version": 1, "kind": "model",
             "source": {"platform": PLATFORM, "label": label, "library": f"user:{options.group}", "import": dict(origin, credit=options.credit)},
             "mesh": mesh, "bounds": meshmod.bounds(mesh)}
    model["id"] = content_id("model", {"label": label, "mesh": mesh})
    result.models[model["id"]] = model
    return result


def read_file(path: Path, options: ImportOptions | None = None) -> ImportResult:
    """Read one model or picture into an ``ImportResult`` (nothing is stored yet)."""

    options = options or ImportOptions()
    path = Path(path)
    if not GROUPS.match(options.group or ""):
        raise ImportProblem("the group name must be 1-40 characters without / \\ : * ? \" < > |")
    if len(options.credit) > 500:
        raise ImportProblem("the credit and licence text must be at most 500 characters")
    if options.units not in UNITS:
        raise ImportProblem(f"units must be one of: {', '.join(UNITS)}")
    if options.shape not in SHAPES:
        raise ImportProblem(f"a picture can become: {', '.join(SHAPES)}")
    try:
        size = path.stat().st_size
    except OSError as error:
        raise ImportProblem(f"{path.name} cannot be opened ({error.strerror or error})") from error
    if not path.is_file():
        raise ImportProblem(f"{path.name} is not a file")
    if size > LIMITS["file_bytes"]:
        raise ImportProblem(f"{path.name} is larger than {LIMITS['file_bytes'] // (1024 * 1024)} MB")
    suffix = path.suffix.lower()
    try:
        if suffix == ".obj":
            result = read_obj(path, options)
        elif suffix in (".gltf", ".glb"):
            result = read_gltf(path, options)
        elif suffix in IMAGE_FORMATS:
            result = read_image(path, options)
        else:
            raise ImportProblem(f"{path.name}: Studio imports {', '.join(MODEL_FORMATS)} models and {', '.join(IMAGE_FORMATS)} pictures. "
                                "Other 3D formats (FBX, 3DS, BLEND, DAE) can be converted to glTF with Blender (File > Export > glTF 2.0)")
    except (OSError, UnicodeDecodeError, meshmod.MeshError, imaging.ImageError) as error:
        raise ImportProblem(f"{path.name} could not be imported: {error}") from error
    if not result.models and not result.textures:
        raise ImportProblem(f"{path.name} contains no triangles or pictures Studio can use")
    if len(result.textures) > LIMITS["textures"]:
        raise ImportProblem(f"{path.name} uses {len(result.textures)} textures; up to {LIMITS['textures']} are supported")
    return result


# ------------------------------------------------------------------- library --

class UserLibrary:
    """The user's imported models and textures, kept as a Studio catalog in ``directory/catalog``."""

    def __init__(self, directory: Path):
        self.directory = Path(directory)

    @property
    def root(self) -> Path:
        return self.directory / "catalog"

    def exists(self) -> bool:
        return (self.root / "catalog.json").is_file()

    def _library(self):
        from tools.btga_assets.studio import StudioLibrary

        return StudioLibrary.open(self.root) if self.exists() else None

    def entries(self) -> tuple[list[dict], list[dict]]:
        """(model catalog entries, texture catalog entries) with labels, groups and credits."""

        library = self._library()
        if library is None:
            return [], []
        models = []
        for entry in library.catalog["models"]:
            record = json.loads(library._read(entry["file"]))
            models.append({**entry, "label": record["source"].get("label"), "group": (record["source"].get("library") or "user:").split(":", 1)[1],
                           "credit": record["source"].get("import", {}).get("credit", ""), "file_name": record["source"].get("import", {}).get("file"),
                           "textures": sorted({m["texture"] for m in record["mesh"]["materials"] if m.get("texture")})})
        textures = [{**t, "group": ((t.get("source") or {}).get("library") or "user:").split(":", 1)[1],
                     "credit": ((t.get("source") or {}).get("import") or {}).get("credit", "")} for t in library.catalog["textures"]]
        return models, textures

    def records(self) -> tuple[dict[str, dict], dict[str, dict]]:
        library = self._library()
        if library is None:
            return {}, {}
        models = {}
        for entry in library.catalog["models"]:
            models[entry["id"]] = json.loads(library._read(entry["file"]))
        textures = {}
        for entry in library.catalog["textures"]:
            texture = library.texture(entry["id"])
            textures[entry["id"]] = {**{k: v for k, v in entry.items() if k != "file"}, "rgba": texture.rgba}
        return models, textures

    def add(self, result: ImportResult) -> list[str]:
        """Store an import; returns the new IDs (already-present content is kept, not duplicated)."""

        models, textures = self.records()
        added = [k for k in sorted(result.models) if k not in models] + [k for k in sorted(result.textures) if k not in textures]
        models.update({k: v for k, v in result.models.items() if k not in models})
        textures.update({k: v for k, v in result.textures.items() if k not in textures})
        self._write(models, textures)
        return added

    def remove(self, identifiers: list[str]) -> list[str]:
        """Remove models and textures; textures still used by a kept model stay.  Returns what was removed."""

        models, textures = self.records()
        gone = [k for k in identifiers if k in models]
        for key in gone:
            del models[key]
        used = {m.get("texture") for record in models.values() for m in record["mesh"]["materials"]}
        for key in identifiers:
            if key in textures and key not in used:
                del textures[key]
                gone.append(key)
        self._write(models, textures)
        return sorted(gone)

    def add_package(self, data: bytes) -> list[str]:
        """Restore the imported resources a content package carries (for projects shared between computers)."""

        from .package import read_package

        package = read_package(data)
        result = ImportResult("package", hashlib.sha256(data).hexdigest(), "package")
        for model_id, model in package.models.items():
            if not model_id.startswith(f"{PLATFORM}:"):
                continue
            source = model.get("source") or {"platform": PLATFORM, "label": model_id.rsplit(":", 1)[-1], "library": f"user:{'From packages'}"}
            result.models[model_id] = {"id": model_id, "schema": "btga-neutral", "schema_version": 1, "kind": "model", "source": source,
                                       "mesh": model["mesh"], "bounds": model.get("bounds") or meshmod.bounds(model["mesh"])}
        texture_meta = {t["id"]: t for t in package.manifest.get("user_textures", [])}
        for stem, (width, height, rgba) in package.textures.items():
            if not stem.startswith(f"{PLATFORM}_texture_"):
                continue
            texture_id = f"{PLATFORM}:texture:{stem[len(PLATFORM) + 9:]}"
            meta = texture_meta.get(texture_id, {})
            result.textures[texture_id] = {"id": texture_id, "width": width, "height": height, "rgba": rgba, "format": "rgba8",
                                           "wrap": meta.get("wrap") or {"s": "repeat", "t": "repeat"}, "alpha": imaging.alpha_kind(rgba),
                                           "label": meta.get("label") or stem, **({"sky": meta["sky"]} if meta.get("sky") else {}),
                                           "source": meta.get("source") or {"platform": PLATFORM, "label": stem, "library": "user:From packages"}}
        return self.add(result)

    def _write(self, models: dict[str, dict], textures: dict[str, dict]) -> None:
        from tools.btga_assets import export as ex

        size = sum(len(t["rgba"]) for t in textures.values())
        if size > LIMITS["library_bytes"]:
            raise ImportProblem("the imported library would exceed 2 GB of textures; remove some imports first")
        self.directory.mkdir(parents=True, exist_ok=True)
        partial = self.directory / "catalog.partial"
        if partial.exists():
            shutil.rmtree(partial)
        exporter = ex.Exporter(partial)
        exporter.sources.append({"platform": PLATFORM, "title": "Your imported models and textures"})
        exporter.models.update(models)
        exporter.textures.update(textures)
        exporter.finish(write_model_glb=False)
        old = self.directory / "catalog.old"
        if old.exists():
            shutil.rmtree(old)
        if self.root.exists():
            self.root.replace(old)
        partial.replace(self.root)
        if old.exists():
            shutil.rmtree(old)
