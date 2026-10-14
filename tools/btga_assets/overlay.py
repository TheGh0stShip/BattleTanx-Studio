"""Non-destructive project overlays for BTGA Studio map editing.

Source scene records are immutable.  Every edit is stored in an overlay
that references stable source IDs, and ``apply`` produces an edited copy of
the scene in which each touched record keeps its ``source`` block and gains
an ``overlay`` provenance block.  Nothing here writes ROM or disc images.

Overlay file (``btga-overlay`` v1), saved as sorted, indented JSON::

    {"schema": "btga-overlay", "schema_version": 1,
     "scene": <scene id>, "platform": <platform>,
     "next_user_index": <n>, "edits": [<edit>, ...]}

Edits (applied in order):
    add_marker      {"op", "marker": typed marker}          new ID "user:<scene hash>:<n>"
    add_instance    {"op", "instance": {model, translation, yaw, category}}  a user-placed visual prop
    move            {"op", "target", "translation"?, "yaw"?}
    set_field       {"op", "target", "field", "value"}
    delete          {"op", "target"}                        user-created records only
    disable/enable  {"op", "target"}                        hides a source record non-destructively
    add_collision   {"op", "collision": collision shape}
    set_collision   {"op", "target", "min"?, "max"?, "vertical"?}
    set_minimap     {"op", "minimap": {"mode": "source", "variant"} | {"mode": "generated", ...}}
    set_nav_link    {"op", "link", "targets"}

``validate`` returns structured errors: {"code", "target", "message", "details"}.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math

from .semantics import EVIDENCE, field_errors

OVERLAY_SCHEMA = "btga-overlay"
OVERLAY_VERSION = 1
OPS = ("add_marker", "add_instance", "move", "set_field", "delete", "disable", "enable", "add_collision", "set_collision", "set_minimap", "set_nav_link")

# Marker source kinds each platform's loaders accept; a marker of another
# kind cannot be written back to that platform.
SUPPORTED_KINDS = {
    "n64_btga": {f"ga_kind_{k}" for k in (3, 4, 5, 7, 10, 12, 13, 15, 16, 17, 20, 21, 24, 27, 28, 29, 30, 31, 32, 36, 40, 45)},
    "n64_bt1": {f"bt1_marker_{k}" for k in (10, 21, 22, 23, 24, 25)} | {"bt1_type_16", "bt1_type_17"},
    "ps1": {"ps1_kind_13", "ps1_kind_27", "ps1_kind_30"},
}
TEAM_FIELDS = ("team", "team_slot", "owner_slot", "team_index", "slot")
GENERATED_MINIMAP_LIMITS = {"max_width": 72, "max_height": 75}
ERROR_CODES = ("OUT_OF_BOUNDS", "INVALID_TEAM", "MISSING_DEPENDENCY", "COLLISION_OVERLAP", "INVALID_COLLISION",
               "UNSUPPORTED_MARKER", "DUPLICATE_ID", "INVALID_NAV_LINK", "ILLEGAL_FIELD", "UNKNOWN_TARGET",
               "PROTECTED_SOURCE", "INVALID_EDIT", "TEAM_LIMIT", "INVALID_MINIMAP", "UNSUPPORTED_FIELD")
# Targets whose placement records can carry a per-object scale.
SCALABLE_PLATFORMS = {"studio"}


class OverlayError(ValueError):
    pass


def _error(code: str, target, message: str, **details) -> dict:
    assert code in ERROR_CODES
    return {"code": code, "target": target, "message": message, "details": details}


class Overlay:
    def __init__(self, scene_id: str, platform: str, edits: list[dict] | None = None, next_user_index: int = 0):
        self.scene_id = scene_id
        self.platform = platform
        self.edits = list(edits or [])
        self.next_user_index = next_user_index

    # -- construction ------------------------------------------------------
    @classmethod
    def for_scene(cls, scene: dict) -> "Overlay":
        return cls(scene["id"], scene["source"]["platform"])

    def _user_id(self) -> str:
        digest = hashlib.sha256(self.scene_id.encode()).hexdigest()[:12]
        identifier = f"user:{digest}:{self.next_user_index}"
        self.next_user_index += 1
        return identifier

    def add(self, edit: dict) -> dict:
        if edit.get("op") not in OPS:
            raise OverlayError(f"unknown edit {edit.get('op')!r}")
        edit = copy.deepcopy(edit)
        if edit["op"] == "add_marker":
            edit["marker"]["id"] = self._user_id()
        if edit["op"] == "add_collision":
            edit["collision"]["id"] = self._user_id()
        if edit["op"] == "add_instance":
            edit["instance"]["id"] = self._user_id()
        self.edits.append(edit)
        return edit

    def add_marker(self, template: dict, translation, yaw: int = 0, values: dict | None = None) -> str:
        """Add a marker built from a template (typically an existing marker of the wanted kind)."""

        marker = copy.deepcopy(template)
        for key in ("id", "overlay", "disabled"):
            marker.pop(key, None)
        marker["translation"] = [float(v) for v in translation]
        marker["yaw"] = int(yaw) & 0xFFFF
        marker["source"] = {"user_created": True, "template": template.get("id")}
        marker["confidence"] = "proven" if template.get("confidence") in EVIDENCE else "unresolved"
        for name, value in (values or {}).items():
            if name not in marker["fields"]:
                raise OverlayError(f"{template.get('source_kind')} has no field {name!r}")
            marker["fields"][name]["value"] = value
        return self.add({"op": "add_marker", "marker": marker})["marker"]["id"]

    def add_instance(self, model: str, translation, yaw: int = 0, category: str = "model") -> str:
        instance = {"model": model, "translation": [float(v) for v in translation], "yaw": int(yaw) & 0xFFFF, "category": category,
                    "source": {"user_created": True}}
        return self.add({"op": "add_instance", "instance": instance})["instance"]["id"]

    def move(self, target: str, translation=None, yaw: int | None = None) -> None:
        edit = {"op": "move", "target": target}
        if translation is not None:
            edit["translation"] = [float(v) for v in translation]
        if yaw is not None:
            edit["yaw"] = int(yaw) & 0xFFFF
        self.add(edit)

    def set_field(self, target: str, name: str, value) -> None:
        self.add({"op": "set_field", "target": target, "field": name, "value": value})

    def delete(self, target: str) -> None:
        self.add({"op": "delete", "target": target})

    def disable(self, target: str) -> None:
        self.add({"op": "disable", "target": target})

    def enable(self, target: str) -> None:
        self.add({"op": "enable", "target": target})

    def revert(self, count: int = 1) -> list[dict]:
        removed = self.edits[-count:] if count else []
        del self.edits[len(self.edits) - len(removed):]
        return removed

    # -- persistence ---------------------------------------------------------
    def to_json(self) -> str:
        document = {"schema": OVERLAY_SCHEMA, "schema_version": OVERLAY_VERSION, "scene": self.scene_id, "platform": self.platform,
                    "next_user_index": self.next_user_index, "edits": self.edits}
        text = json.dumps(document, sort_keys=True, indent=1, ensure_ascii=True, allow_nan=False)
        _refuse_host_paths(text)
        return text + "\n"

    @classmethod
    def from_json(cls, text: str) -> "Overlay":
        document = json.loads(text)
        if document.get("schema") != OVERLAY_SCHEMA or document.get("schema_version") != OVERLAY_VERSION:
            raise OverlayError("not a btga-overlay v1 document")
        for edit in document["edits"]:
            if edit.get("op") not in OPS:
                raise OverlayError(f"unknown edit {edit.get('op')!r}")
        return cls(document["scene"], document["platform"], document["edits"], document["next_user_index"])


def _refuse_host_paths(text: str) -> None:
    for marker in ("/home/", "/Users/", "C:\\\\", "\\\\Users\\\\", "/tmp/", "/mnt/"):
        if marker in text:
            raise OverlayError(f"overlay contains a host path fragment {marker!r}")


# ------------------------------------------------------------------ apply ---


def apply(scene: dict, overlay: Overlay) -> dict:
    """Return an edited copy of ``scene``; the input is never modified."""

    if overlay.scene_id != scene["id"]:
        raise OverlayError(f"overlay targets {overlay.scene_id}, not {scene['id']}")
    out = copy.deepcopy(scene)
    out["overlay"] = {"scene": scene["id"], "edits": len(overlay.edits)}
    index = {}
    for family in ("markers", "collision", "instances"):
        for record in out.get(family, []):
            index[record["id"]] = (family, record)

    def touch(record: dict, number: int, op: str) -> None:
        record.setdefault("overlay", {"edits": []})["edits"].append({"edit": number, "op": op})

    for number, edit in enumerate(overlay.edits):
        op = edit["op"]
        if op == "add_marker":
            record = copy.deepcopy(edit["marker"])
            touch(record, number, op)
            out["markers"].append(record)
            index[record["id"]] = ("markers", record)
        elif op == "add_collision":
            record = copy.deepcopy(edit["collision"])
            record.setdefault("source", {})["user_created"] = True
            touch(record, number, op)
            out["collision"].append(record)
            index[record["id"]] = ("collision", record)
        elif op == "set_minimap":
            out["minimap"] = {**out.get("minimap", {}), "override": copy.deepcopy(edit["minimap"])}
        elif op == "set_nav_link":
            links = (out.get("navigation") or {}).get("links", [])
            if 0 <= edit["link"] < len(links):
                links[edit["link"]]["targets"] = list(edit["targets"])
                links[edit["link"]].setdefault("overlay", {"edits": []})["edits"].append({"edit": number, "op": op})
        elif op == "add_instance":
            record = copy.deepcopy(edit["instance"])
            touch(record, number, op)
            out["instances"].append(record)
            index[record["id"]] = ("instances", record)
        else:
            if edit["target"] not in index:
                continue  # reported by validate
            family, record = index[edit["target"]]
            if op == "delete" and record.get("source", {}).get("user_created"):
                out[family] = [r for r in out[family] if r["id"] != edit["target"]]
                del index[edit["target"]]
                continue
            apply_to_record(record, family, edit, number)
    return out


def apply_to_record(record: dict, family: str, edit: dict, number: int) -> None:
    """Apply one targeted edit to one record in place (shared by apply and the editor's incremental view)."""

    op = edit["op"]
    if op == "move":
        if "translation" in edit:
            record["translation"] = list(edit["translation"])
        if "yaw" in edit:
            record["yaw"] = edit["yaw"]
        if "scale" in edit:
            if edit["scale"] in (None, [1.0, 1.0, 1.0]):
                record.pop("scale", None)
            else:
                record["scale"] = [float(v) for v in edit["scale"]]
    elif op == "set_field" and edit["field"] in record.get("fields", {}):
        record["fields"][edit["field"]]["value"] = edit["value"]
    elif op == "disable":
        record["disabled"] = True
    elif op == "enable":
        record.pop("disabled", None)
    elif op == "set_collision" and family == "collision":
        for key in ("min", "max", "vertical"):
            if key in edit:
                record[key] = copy.deepcopy(edit[key])
    record.setdefault("overlay", {"edits": []})["edits"].append({"edit": number, "op": op})


# --------------------------------------------------------------- validate ---


def _footprint_aabb(shape: dict) -> tuple[float, float, float, float]:
    theta = (shape["yaw"] & 0xFFFF) * 2 * math.pi / 65536
    c, s = math.cos(theta), math.sin(theta)
    corners = [(x, z) for x in (shape["min"][0], shape["max"][0]) for z in (shape["min"][2], shape["max"][2])]
    xs = [shape["translation"][0] + x * c + z * s for x, z in corners]
    zs = [shape["translation"][2] - x * s + z * c for x, z in corners]
    return min(xs), min(zs), max(xs), max(zs)


def _vertical(shape: dict) -> tuple[float, float] | None:
    lo, hi = shape["min"][1], shape["max"][1]
    if lo is None or hi is None:
        return None
    return shape["translation"][1] + lo, shape["translation"][1] + hi


def validate(scene: dict, overlay: Overlay, known_models: set[str] | None = None, known_textures: set[str] | None = None,
             target_platform: str | None = None, edited: dict | None = None, strict: bool = False) -> list[dict]:
    """Structured errors for an overlay against its scene (and an optional target platform).

    ``strict`` refuses Studio-native record kinds (for a future write-back into
    game data); content packages accept them.  ``edited`` may pass an already-applied scene (equal to apply(scene, overlay))
    so callers that maintain one incrementally avoid a second full copy.
    """

    errors: list[dict] = []
    platform = target_platform or scene["source"]["platform"]
    source_ids = {r["id"] for family in ("markers", "collision", "instances") for r in scene.get(family, [])}
    seen = set()
    for record in [r for family in ("markers", "collision", "instances") for r in scene.get(family, [])]:
        if record["id"] in seen:
            errors.append(_error("DUPLICATE_ID", record["id"], "two source records share this ID"))
        seen.add(record["id"])
    user_ids = set()
    for number, edit in enumerate(overlay.edits):
        op = edit.get("op")
        if op not in OPS:
            errors.append(_error("INVALID_EDIT", None, f"edit {number}: unknown op {op!r}"))
            continue
        if op in ("add_marker", "add_collision", "add_instance"):
            record = edit[{"add_marker": "marker", "add_collision": "collision", "add_instance": "instance"}[op]]
            if record["id"] in source_ids or record["id"] in user_ids:
                errors.append(_error("DUPLICATE_ID", record["id"], f"edit {number}: ID already in use"))
            user_ids.add(record["id"])
        elif op == "delete":
            if edit["target"] in source_ids:
                errors.append(_error("PROTECTED_SOURCE", edit["target"], f"edit {number}: source records can only be disabled, not deleted"))
            elif edit["target"] not in user_ids:
                errors.append(_error("UNKNOWN_TARGET", edit["target"], f"edit {number}: no such record"))
        elif op in ("move", "set_field", "disable", "enable", "set_collision"):
            if edit["target"] not in source_ids and edit["target"] not in user_ids:
                errors.append(_error("UNKNOWN_TARGET", edit["target"], f"edit {number}: no such record"))
        elif op == "set_minimap":
            minimap = edit["minimap"]
            if minimap.get("mode") == "source":
                variants = {s["variant"] for s in scene.get("minimap", {}).get("selections", [])}
                if minimap.get("variant") not in variants:
                    errors.append(_error("INVALID_MINIMAP", None, f"edit {number}: no source minimap for variant {minimap.get('variant')!r}"))
            elif minimap.get("mode") == "generated":
                w, h = minimap.get("width", 0), minimap.get("height", 0)
                if not (0 < w <= GENERATED_MINIMAP_LIMITS["max_width"] and 0 < h <= GENERATED_MINIMAP_LIMITS["max_height"]):
                    errors.append(_error("INVALID_MINIMAP", None, f"edit {number}: generated size {w}x{h} exceeds the HUD's 72x75 images"))
                if known_textures is not None and minimap.get("texture") and minimap["texture"] not in known_textures:
                    errors.append(_error("MISSING_DEPENDENCY", None, f"edit {number}: texture {minimap['texture']} is not in the catalog"))
            else:
                errors.append(_error("INVALID_MINIMAP", None, f"edit {number}: mode must be 'source' or 'generated'"))
    if edited is None:
        edited = apply(scene, overlay) if not any(e["code"] in ("INVALID_EDIT",) for e in errors) else scene
    touched = {r["id"] for family in ("markers", "collision", "instances") for r in edited.get(family, []) if r.get("overlay")}
    bounds = (edited.get("bounds") or {}).get("map")
    for record in edited.get("instances", []) + edited.get("markers", []):
        if record["id"] in touched and record.get("scale") and platform not in SCALABLE_PLATFORMS:
            errors.append(_error("UNSUPPORTED_FIELD", record["id"], f"scale cannot be stored on {platform}", field="scale"))
    for record in edited.get("instances", []):
        if record["id"] not in touched or record.get("disabled"):
            continue
        if known_models is not None and record.get("model") not in known_models:
            errors.append(_error("MISSING_DEPENDENCY", record["id"], f"model {record.get('model')} is not in the catalog"))
        if bounds:
            x, _, z = record["translation"]
            if not (bounds["x"][0] <= x <= bounds["x"][1] and bounds["z"][0] <= z <= bounds["z"][1]):
                errors.append(_error("OUT_OF_BOUNDS", record["id"], f"({x}, {z}) lies outside the map bounds", bounds=bounds))
    bounds = (edited.get("bounds") or {}).get("map")
    team_counts: dict[int, int] = {}
    for record in edited.get("markers", []):
        if record["id"] not in touched or record.get("disabled"):
            continue
        kind = record.get("source_kind") or ""
        # Studio-native kinds travel in PC-port content packages on every platform.
        if kind not in SUPPORTED_KINDS.get(platform, set()) and not (kind.startswith("studio_") and not strict):
            errors.append(_error("UNSUPPORTED_MARKER", record["id"], f"{record.get('source_kind')} cannot be stored on {platform}",
                                 platform=platform))
        for name, spec in record.get("fields", {}).items():
            problems = field_errors(name, spec, spec["value"])
            code = "INVALID_TEAM" if name in TEAM_FIELDS else "ILLEGAL_FIELD"
            for problem in problems:
                errors.append(_error(code, record["id"], problem, field=name))
        if bounds:
            x, _, z = record["translation"]
            if not (bounds["x"][0] <= x <= bounds["x"][1] and bounds["z"][0] <= z <= bounds["z"][1]):
                errors.append(_error("OUT_OF_BOUNDS", record["id"], f"({x}, {z}) lies outside the map bounds", bounds=bounds))
        if known_models is not None:
            for dependency in record.get("dependencies", []):
                if dependency.get("kind") == "model" and dependency.get("id") and dependency["id"] not in known_models:
                    errors.append(_error("MISSING_DEPENDENCY", record["id"], f"model {dependency['id']} is not in the catalog"))
    touched_teams = set()
    for record in edited.get("markers", []):
        limit = record.get("restrictions", {}).get("max_per_team")
        if limit is None or "team" not in record.get("fields", {}) or record.get("disabled"):
            continue
        team = record["fields"]["team"]["value"]
        team_counts[team] = team_counts.get(team, 0) + 1
        if record["id"] in touched:
            touched_teams.add(team)
    for team in sorted(touched_teams):
        if team_counts[team] > 5:
            errors.append(_error("TEAM_LIMIT", None, f"team {team} has {team_counts[team]} spawn slots; the loader keeps at most 5", team=team))
    shapes = [s for s in edited.get("collision", []) if not s.get("disabled")]
    for shape in shapes:
        if shape["id"] not in touched:
            continue
        lo, hi = shape["min"], shape["max"]
        if lo[0] >= hi[0] or lo[2] >= hi[2] or (lo[1] is not None and hi[1] is not None and lo[1] > hi[1]):
            errors.append(_error("INVALID_COLLISION", shape["id"], "collision extent is empty or inverted"))
            continue
        if (lo[1] is None) != (hi[1] is None) or (lo[1] is not None and shape.get("vertical", {}).get("status") != "proven"):
            errors.append(_error("INVALID_COLLISION", shape["id"], "heights are only allowed on platforms whose collision stores them"))
            continue
        box = _footprint_aabb(shape)
        vertical = _vertical(shape)
        for other in shapes:
            if other["id"] == shape["id"]:
                continue
            ob = _footprint_aabb(other)
            if box[0] < ob[2] and ob[0] < box[2] and box[1] < ob[3] and ob[1] < box[3]:
                ov = _vertical(other)
                if vertical is None or ov is None or (vertical[0] < ov[1] and ov[0] < vertical[1]):
                    errors.append(_error("COLLISION_OVERLAP", shape["id"], f"overlaps {other['id']}", other=other["id"]))
                    break
        if bounds:
            if box[0] < bounds["x"][0] or box[2] > bounds["x"][1] or box[1] < bounds["z"][0] or box[3] > bounds["z"][1]:
                errors.append(_error("OUT_OF_BOUNDS", shape["id"], "collision extends outside the map bounds", bounds=bounds))
    navigation = edited.get("navigation") or {}
    count = navigation.get("point_count", len(navigation.get("points", [])))
    for link in navigation.get("links", []):
        if not link.get("overlay"):
            continue
        for target in link["targets"]:
            if not isinstance(target, int) or not 0 <= target < count:
                errors.append(_error("INVALID_NAV_LINK", f"link:{link['index']}", f"target {target} is not one of the {count} points"))
    return errors
