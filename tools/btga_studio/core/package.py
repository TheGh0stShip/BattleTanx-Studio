"""BattleTanx Studio custom-content packages (``btga-content-package`` v1).

A package is what the PC port loads; Studio never patches a ROM.  It is a
ZIP archive written deterministically (entries sorted, stored uncompressed,
fixed 1980-01-01 timestamps, no extra fields), so the same project yields
byte-identical packages on every platform.  Layout:

    manifest.json                schema, versions, project identity, source hashes, file list with SHA-256
    project.json                 metadata, campaigns, missions, multiplayer, minigames, tanks, gangs
    maps/<key>.json              one per map (see MAP CONTRACT in docs/CONTENT_PACKAGE.md)
    resources/models/<id>.json   Studio-original models used by the package (neutral mesh)
    resources/textures/<id>.rgba Studio-original textures: u16 width, u16 height (LE), RGBA8 rows

Game-derived models and textures are referenced by content ID only; the
runtime resolves them from its own locally extracted data and checks the
source hashes in the manifest.  Raw and unresolved source fields travel
unchanged inside records (``raw_hex``, ``unknown_bytes``).
"""

from __future__ import annotations

import copy
import hashlib
import io
import json
import re
import struct
import zipfile
from dataclasses import dataclass
from pathlib import Path

from tools.btga_assets import overlay as overlay_module

from . import diagnostics as diag
from .project import PROJECT_VERSION, StudioProject

PACKAGE_SCHEMA = "btga-content-package"
PACKAGE_VERSION = 1
STARTER_PLATFORMS = ("studio",)
LIMITS = {"package_bytes": 256 * 1024 * 1024, "file_bytes": 64 * 1024 * 1024, "files": 20_000, "maps": 256, "records_per_map": 200_000}
TIMESTAMP = (1980, 1, 1, 0, 0, 0)
SAFE_NAME = re.compile(r"^(manifest\.json|project\.json|maps/[a-z0-9_]{1,48}\.json|resources/(models|textures)/[a-z0-9_]{1,128}\.(json|rgba))$")


class PackageError(ValueError):
    pass


@dataclass
class PackageReport:
    errors: list[dict]
    warnings: list[dict]

    @property
    def ok(self) -> bool:
        return not self.errors


def _json(data) -> bytes:
    return (json.dumps(data, sort_keys=True, indent=1, ensure_ascii=False, allow_nan=False) + "\n").encode("utf-8")


def _stem(identifier: str) -> str:
    return identifier.replace(":", "_")


# ---------------------------------------------------------------- validation --

TARGET_KINDS = {
    "studio": {"studio_"},
    "n64_btga": {"ga_kind_", "studio_"},
    "n64_bt1": {"bt1_", "studio_"},
    "ps1": {"ps1_kind_", "studio_"},
}


def validate_for_export(project: StudioProject, library) -> PackageReport:
    """Everything that would make the package wrong or unloadable, with object and field."""

    errors, warnings = [], []
    for problem in project.problems(library):
        errors.append({**problem, "map": None})
    if not project.maps:
        errors.append({"code": "EMPTY_PROJECT", "target": "maps", "field": None, "map": None, "message": "the project has no maps"})
    if len(project.maps) > LIMITS["maps"]:
        errors.append({"code": "LIMIT", "target": "maps", "field": None, "map": None, "message": f"more than {LIMITS['maps']} maps"})
    target = project.project["target"]["game"]
    for key, entry in sorted(project.maps.items()):
        try:
            document = project.open_map(key, library)
        except Exception as error:  # mismatched or missing source
            errors.append({"code": "MAP_UNAVAILABLE", "target": f"maps:{key}", "field": "scene", "map": key, "message": str(error)})
            continue
        platform = document.scene.base["source"].get("platform")
        if entry.kind == "imported" and target not in ("studio", platform):
            errors.append({"code": "UNSUPPORTED_FEATURE", "target": f"maps:{key}", "field": "scene", "map": key,
                           "message": f"a {platform} map cannot be part of a {target} project"})
        for item in diag.collect(document, library):
            record = {"code": item.code, "target": item.target, "field": getattr(item, "field", None), "map": key, "message": item.message}
            (errors if item.severity == "error" else warnings).append(record)
        allowed = TARGET_KINDS.get(target if target != "studio" else platform, {"studio_"}) | ({"studio_"} if platform == "studio" else set())
        for family, record in document.scene.records("markers"):
            if not record.get("overlay"):
                continue
            kind = record.get("source_kind", "")
            if not any(kind.startswith(prefix) for prefix in allowed):
                errors.append({"code": "UNSUPPORTED_FEATURE", "target": record["id"], "field": "source_kind", "map": key,
                               "message": f"{kind} records cannot be loaded for target {target}"})
        if len(document.scene.view) > LIMITS["records_per_map"]:
            errors.append({"code": "LIMIT", "target": f"maps:{key}", "field": None, "map": key, "message": "too many records"})
    if target != "studio" and (project.minigames or project.gangs):
        warnings.append({"code": "UNSUPPORTED_FEATURE", "target": "project", "field": "minigames/gangs", "map": None,
                         "message": f"minigames and gangs are Studio extensions; the {target} runtime path may ignore them"})
    key = lambda item: (item["map"] or "", item["code"], str(item["target"]), str(item["field"]), item["message"])  # noqa: E731
    return PackageReport(sorted(errors, key=key), sorted(warnings, key=key))


# ------------------------------------------------------------------- export --

def _map_document(key: str, entry, document, library) -> tuple[dict, set[str]]:
    scene = document.scene
    edited = scene.edited_scene(document.overlay)
    used_models = set()
    base = scene.base
    out = {"key": key, "kind": entry.kind, "title": entry.title, "platform": base["source"].get("platform"),
           "bounds": copy.deepcopy((base.get("bounds") or {}).get("map")),
           "environment": copy.deepcopy(base.get("environment") or []),
           "minimap": {"config": copy.deepcopy(entry.minimap), "source": copy.deepcopy(base.get("minimap") or {})},
           "hidden_in_editor": sorted(document.hidden)}
    if entry.kind == "blank":
        out["records"] = {family: [r for r in edited[family] if not r.get("disabled")] for family in ("instances", "markers", "collision")}
        out["navigation"] = _navigation_from_markers(out["records"]["markers"])
    else:
        out["source"] = {"scene": scene.level_id, "fingerprint": entry.source_fingerprint}
        out["overlay"] = json.loads(document.overlay.to_json())
        out["navigation_edits"] = [e for e in document.overlay.edits if e["op"] == "set_nav_link"]
    for family, record in scene.records():
        if family == "instances":
            used_models.add(record["model"])
        for dependency in record.get("dependencies", []):
            if dependency.get("kind") == "model" and dependency.get("id"):
                used_models.add(dependency["id"])
    return out, used_models


def _navigation_from_markers(markers: list[dict]) -> dict:
    nodes = [m for m in markers if m.get("category") == "navigation_node"]
    points = [{"id": m["id"], "x": m["translation"][0], "y": m["translation"][1], "z": m["translation"][2]} for m in nodes]
    links = []
    for m in nodes:
        for name in sorted(m.get("fields", {})):
            if name.startswith("link_") and m["fields"][name]["value"]:
                links.append([m["id"], m["fields"][name]["value"]])
    return {"points": points, "links": sorted(links)}


def build_package(project: StudioProject, library, studio_version: str = "0") -> bytes:
    """Validate and build the package bytes; raises PackageError listing blocking errors."""

    report = validate_for_export(project, library)
    if not report.ok:
        details = "; ".join(f"{e['code']} {e['target']}{'.' + e['field'] if e['field'] else ''}: {e['message']}" for e in report.errors[:20])
        raise PackageError(f"{len(report.errors)} validation error(s): {details}")
    project.compute_dependencies(library)
    files: dict[str, bytes] = {}
    models_used: set[str] = set()
    for key, entry in sorted(project.maps.items()):
        document = project.open_map(key, library)
        data, used = _map_document(key, entry, document, library)
        models_used |= used
        files[f"maps/{key}.json"] = _json(data)
    for tank in project.tanks.values():
        if isinstance(tank.get("model"), str):
            models_used.add(tank["model"])
    textures_used = set()
    for model_id in sorted(models_used):
        if not model_id.startswith(tuple(f"{p}:" for p in STARTER_PLATFORMS)) or model_id not in library._models:
            continue
        model = library.model(model_id)
        files[f"resources/models/{_stem(model_id)}.json"] = _json({"id": model_id, "mesh": model.mesh, "bounds": model.bounds})
        textures_used |= {m.get("texture") for m in model.mesh["materials"] if m.get("texture")}
    for texture_id in sorted(textures_used):
        texture = library.texture(texture_id)
        files[f"resources/textures/{_stem(texture_id)}.rgba"] = struct.pack("<HH", texture.width, texture.height) + texture.rgba
    meta = project.as_dict()
    files["project.json"] = _json({"project": meta["project"], "campaigns": meta["campaigns"], "missions": meta["missions"],
                                   "multiplayer": meta["multiplayer"], "minigames": meta["minigames"], "tanks": meta["tanks"],
                                   "gangs": meta["gangs"], "extra": meta.get("extra", {}),
                                   "unknown": {k: v for k, v in meta.items() if k not in ("schema", "schema_version", "project", "dependencies",
                                                                                          "maps", "campaigns", "missions", "multiplayer",
                                                                                          "minigames", "tanks", "gangs", "extra")}})
    manifest = {
        "schema": PACKAGE_SCHEMA, "schema_version": PACKAGE_VERSION, "studio_version": studio_version,
        "project": {"id": project.project["id"], "title": project.project["title"], "target": project.project["target"]},
        "schema_versions": {"project": PROJECT_VERSION, "overlay": overlay_module.OVERLAY_VERSION, "semantics": 3,
                            "starter": project.dependencies.get("starter")},
        "sources": project.dependencies.get("sources", []),
        "game_resources": sorted(m for m in models_used if not m.startswith(tuple(f"{p}:" for p in STARTER_PLATFORMS))),
        "files": [{"path": name, "sha256": hashlib.sha256(data).hexdigest(), "size": len(data)} for name, data in sorted(files.items())],
        "warnings": report.warnings,
    }
    files["manifest.json"] = _json(manifest)
    return _zip(files)


def _zip(files: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_STORED) as archive:
        for name in sorted(files):
            if not SAFE_NAME.match(name):
                raise PackageError(f"internal error: unsafe package path {name!r}")
            info = zipfile.ZipInfo(name, TIMESTAMP)
            info.compress_type = zipfile.ZIP_STORED
            info.external_attr = 0o644 << 16
            info.create_system = 0
            archive.writestr(info, files[name])
    data = buffer.getvalue()
    if len(data) > LIMITS["package_bytes"]:
        raise PackageError(f"package is {len(data)} bytes; the limit is {LIMITS['package_bytes']}")
    return data


def write_package(path: Path, project: StudioProject, library, studio_version: str = "0") -> Path:
    data = build_package(project, library, studio_version)
    Path(path).write_bytes(data)
    return Path(path)


# ------------------------------------------------------------------- reading --

@dataclass
class Package:
    manifest: dict
    project: dict
    maps: dict[str, dict]
    models: dict[str, dict]
    textures: dict[str, tuple[int, int, bytes]]


def read_package(data: bytes) -> Package:
    """Strictly read and verify a package (what a runtime loader must do)."""

    if len(data) > LIMITS["package_bytes"]:
        raise PackageError("package exceeds the size limit")
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as error:
        raise PackageError(f"not a package: {error}") from error
    infos = archive.infolist()
    if len(infos) > LIMITS["files"]:
        raise PackageError("too many files")
    names = [info.filename for info in infos]
    if len(set(names)) != len(names):
        raise PackageError("duplicate file names")
    total_size = 0
    for info in infos:
        if not SAFE_NAME.match(info.filename):
            raise PackageError(f"unsafe or unexpected path {info.filename!r}")
        if (info.external_attr >> 16) & 0o170000 == 0o120000:
            raise PackageError(f"{info.filename} is a symbolic link")
        if info.file_size > LIMITS["file_bytes"] or info.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED):
            raise PackageError(f"{info.filename} is too large or uses an unsupported compression")
        total_size += info.file_size
        if total_size > LIMITS["package_bytes"]:
            raise PackageError("package expands beyond the total size limit")
    if "manifest.json" not in names:
        raise PackageError("missing manifest.json")

    def read(name: str) -> bytes:
        with archive.open(name) as handle:
            content = handle.read(LIMITS["file_bytes"] + 1)
        if len(content) > LIMITS["file_bytes"]:
            raise PackageError(f"{name} expands beyond the file limit")
        return content

    try:
        manifest = json.loads(read("manifest.json"))
    except ValueError as error:
        raise PackageError(f"manifest is not JSON: {error}") from error
    if manifest.get("schema") != PACKAGE_SCHEMA:
        raise PackageError("not a BattleTanx content package")
    version = manifest.get("schema_version")
    if not isinstance(version, int) or version > PACKAGE_VERSION:
        raise PackageError(f"package schema {version} is newer than this reader supports ({PACKAGE_VERSION})")
    entries = manifest.get("files")
    if not isinstance(entries, list) or any(
        not isinstance(entry, dict)
        or not isinstance(entry.get("path"), str)
        or not isinstance(entry.get("size"), int)
        or entry["size"] < 0
        or not isinstance(entry.get("sha256"), str)
        or not re.fullmatch(r"[0-9a-f]{64}", entry["sha256"])
        for entry in entries
    ):
        raise PackageError("the manifest file list is malformed")
    listed = {entry["path"]: entry for entry in entries}
    if len(listed) != len(entries):
        raise PackageError("the manifest contains duplicate file entries")
    if set(listed) != set(names) - {"manifest.json"}:
        raise PackageError("the manifest file list does not match the archive")
    contents = {}
    for name, entry in sorted(listed.items()):
        content = read(name)
        if len(content) != entry["size"] or hashlib.sha256(content).hexdigest() != entry["sha256"]:
            raise PackageError(f"{name} does not match its manifest hash")
        contents[name] = content
    try:
        project = json.loads(contents["project.json"])
        maps = {name[5:-5]: json.loads(content) for name, content in contents.items() if name.startswith("maps/")}
        models = {json.loads(content)["id"]: json.loads(content) for name, content in contents.items() if name.startswith("resources/models/")}
    except (KeyError, ValueError) as error:
        raise PackageError(f"malformed package content: {error}") from error
    textures = {}
    for name, content in contents.items():
        if name.startswith("resources/textures/"):
            if len(content) < 4:
                raise PackageError(f"{name} is truncated")
            width, height = struct.unpack_from("<HH", content, 0)
            if width == 0 or height == 0 or len(content) != 4 + width * height * 4:
                raise PackageError(f"{name} has inconsistent dimensions")
            textures[name.rsplit("/", 1)[1][:-5]] = (width, height, content[4:])
    for key, map_data in maps.items():
        bounds = map_data.get("bounds")
        if bounds is not None:
            for axis in ("x", "z"):
                lo, hi = bounds[axis]
                if not (isinstance(lo, (int, float)) and isinstance(hi, (int, float)) and lo < hi and abs(lo) <= 1e6 and abs(hi) <= 1e6):
                    raise PackageError(f"map {key}: invalid bounds on {axis}")
        for family, records in (map_data.get("records") or {}).items():
            for record in records:
                position = record.get("translation")
                if not (isinstance(position, list) and len(position) == 3 and all(isinstance(v, (int, float)) and abs(v) <= 1e6 for v in position)):
                    raise PackageError(f"map {key}: record {record.get('id')} has an invalid position")
                if bounds and not (bounds["x"][0] <= position[0] <= bounds["x"][1] and bounds["z"][0] <= position[2] <= bounds["z"][1]):
                    raise PackageError(f"map {key}: record {record.get('id')} lies outside the map bounds")
    return Package(manifest, project, maps, models, textures)
