"""Validation diagnostics for the editor's output panel.

Combines overlay.validate with editor checks: dangling model/texture
references, spawn points inside collision, overlapping team starts,
spawner masks, minimap transforms and edits that touch unresolved records.
Every diagnostic names its target record so the panel can select and frame it.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass

import numpy as np

from tools.btga_assets import overlay as overlay_module

from . import geometry as g
from .document import is_read_only

SPAWN_CATEGORIES = {"team_spawn_slot", "team_start", "team_point_a", "team_point_b", "multiplayer_structure"}
START_SPACING = 64.0


@dataclass(frozen=True)
class Diagnostic:
    severity: str  # error | warning
    code: str
    message: str
    target: str | None
    field: str | None = None

    def as_dict(self) -> dict:
        return asdict(self)


def _inside(shape: dict, point) -> bool:
    local = g.to_local(point, shape["translation"], shape["yaw"])
    return shape["min"][0] <= local[0] <= shape["max"][0] and shape["min"][2] <= local[2] <= shape["max"][2]


def collect(document, library) -> list[Diagnostic]:
    scene = document.scene
    out: list[Diagnostic] = []
    known_models = set(library._models)
    edited = {**scene.base, **{family: [r for f, r in scene.records(family)] for family in ("instances", "markers", "collision")},
              "navigation": getattr(scene, "navigation", scene.base.get("navigation"))}
    for error in overlay_module.validate(scene.base, document.overlay, known_models, set(library.textures()), edited=edited):
        severity = "warning" if error["code"] in ("COLLISION_OVERLAP",) else "error"
        out.append(Diagnostic(severity, error["code"], error["message"], error["target"], (error.get("details") or {}).get("field")))
    seen = set()
    for family, record in scene.records():
        if record["id"] in seen:
            out.append(Diagnostic("error", "DUPLICATE_ID", "two records share this ID", record["id"]))
        seen.add(record["id"])
        if family == "instances" and record["model"] not in known_models:
            if str(record["model"]).startswith("user:"):
                out.append(Diagnostic("error", "MISSING_IMPORT", f"imported model {record['model']} is not in your library on this computer; "
                                      "import it again or add a content package that has it (File > Import models and textures)", record["id"]))
            else:
                out.append(Diagnostic("error", "DANGLING_REFERENCE", f"model {record['model']} is not in the catalog", record["id"]))
        if record.get("category") == "teleporter" and not record.get("disabled"):
            target = (record.get("fields", {}).get("exit") or {}).get("value")
            found = scene.record(target) if target else None
            if not target:
                out.append(Diagnostic("error", "TELEPORTER_WITHOUT_EXIT", "choose the teleport exit this pad sends vehicles to", record["id"], "exit"))
            elif found is not None and found[1].get("category") != "teleport_exit":
                out.append(Diagnostic("error", "INVALID_FIELD", f"exit must be a teleport exit, not {found[1].get('category')}", record["id"], "exit"))
        for dependency in record.get("dependencies", []):
            if dependency.get("kind") == "model" and dependency.get("id") and dependency["id"] not in known_models:
                out.append(Diagnostic("error", "DANGLING_REFERENCE", f"model {dependency['id']} is not in the catalog", record["id"]))
        spec = record.get("fields", {}).get("unit_type_mask")
        if spec is not None and (spec["value"] <= 0 or spec["value"] >> 15):
            out.append(Diagnostic("error", "INVALID_SPAWNER_MASK", f"unit-type mask {spec['value']:#x} selects no valid unit type (bits 0-14)",
                                  record["id"], "unit_type_mask"))
        for name, spec_ in record.get("fields", {}).items():
            if spec_.get("type") == "ref" and spec_.get("value") and scene.record(spec_["value"]) is None:
                out.append(Diagnostic("error", "DANGLING_REFERENCE", f"{name} refers to missing record {spec_['value']}", record["id"], name))
    shapes = [r for f, r in scene.records("collision") if not r.get("disabled") and r.get("extras", {}).get("registered", True)]
    starts = []
    for family, record in scene.records("markers"):
        if record.get("disabled") or record.get("category") not in SPAWN_CATEGORIES:
            continue
        point = np.asarray(record["translation"], float)
        for shape in shapes:
            if _inside(shape, point):
                out.append(Diagnostic("warning", "SPAWN_IN_COLLISION", f"spawn point lies inside collision {shape['id']}", record["id"]))
                break
        starts.append((record, point))
    for i, (a, pa) in enumerate(starts):
        for b, pb in starts[i + 1 :]:
            if np.linalg.norm((pa - pb)[[0, 2]]) < START_SPACING:
                out.append(Diagnostic("warning", "OVERLAPPING_STARTS", f"start overlaps {b['id']} (closer than {START_SPACING:.0f} units)", a["id"]))
    minimap = scene.base.get("minimap") or {}
    if minimap.get("status") == "proven" and (not minimap.get("scale") or minimap["scale"] <= 0):
        out.append(Diagnostic("error", "INVALID_MINIMAP", "minimap scale must be positive", None))
    for number, edit in enumerate(document.overlay.edits):
        target = edit.get("target")
        if target and scene.record(target) and is_read_only(scene.record(target)[1]):
            out.append(Diagnostic("error", "UNRESOLVED_MODIFIED", f"edit {number} ({edit['op']}) modifies an unresolved record", target))
    for family, record in scene.records("instances"):
        if not record.get("source", {}).get("user_created"):
            continue
        entry = library._models.get(record["model"])
        root = library.root_for_model(record["model"]) if entry is not None and hasattr(library, "root_for_model") else library.root
        if entry is not None and not (root / entry["file"]).is_file():
            out.append(Diagnostic("error", "MISSING_SOURCE_ASSET", f"model file for {record['model']} is missing", record["id"]))
    unique = []
    keys = set()
    for diagnostic in out:
        key = (diagnostic.code, diagnostic.target, diagnostic.field, diagnostic.message)
        if key not in keys:
            keys.add(key)
            unique.append(diagnostic)
    return sorted(unique, key=lambda d: (d.severity != "error", d.code, d.target or "", d.field or "", d.message))
