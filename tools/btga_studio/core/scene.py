"""Editable view of one neutral scene, with bounded mesh caching and ray picking.

The base scene from the catalog is never modified.  ``EditorScene`` keeps
an edited view in which only records touched by the overlay are copies;
``refresh`` recomputes exactly the records an edit (or an undo) affects,
using overlay.apply_to_record, so the view always equals
overlay.apply(base, overlay) without deep-copying the whole scene.
"""

from __future__ import annotations

import copy
import math
from collections import OrderedDict
from dataclasses import dataclass

import numpy as np

from tools.btga_assets import overlay as overlay_module
from tools.btga_assets.gltf import PLANAR_DISPLAY_HEIGHT

from . import geometry as g

FAMILIES = ("instances", "markers", "collision")
# Tie-break order for equal distances: markers sit on top of geometry.
PICK_PRIORITY = {"markers": 0, "collision": 1, "instances": 2}
MARKER_HALF = 24.0
MESH_CACHE_TRIANGLES = 2_000_000


@dataclass(frozen=True)
class Hit:
    id: str
    family: str
    distance: float
    point: tuple[float, float, float]

    def key(self) -> tuple:
        return (round(self.distance, 4), PICK_PRIORITY[self.family], self.id)


class MeshData:
    """LOD-0 triangles of one model as numpy arrays (local space)."""

    __slots__ = ("model_id", "positions", "triangles", "corners", "lo", "hi", "materials", "triangle_materials", "uvs", "colours")

    def __init__(self, model_id: str, mesh: dict):
        self.model_id = model_id
        triangles = mesh["triangles"]
        if mesh.get("lods"):
            lod0 = [entry for entry in mesh["lods"] if entry["index"] == 0]
            if lod0:
                first, count = lod0[0]["first_triangle"], lod0[0]["triangle_count"]
                triangles = triangles[first : first + count]
        self.positions = np.asarray(mesh["positions"], dtype=np.float64).reshape(-1, 3)
        self.triangles = np.asarray([[c["p"] for c in t["corners"]] for t in triangles], dtype=np.int64).reshape(-1, 3)
        self.materials = mesh["materials"]
        self.triangle_materials = np.asarray([t["material"] for t in triangles], dtype=np.int64)
        self.uvs = np.asarray([[c["uv"] or (0.0, 0.0) for c in t["corners"]] for t in triangles], dtype=np.float32).reshape(-1, 3, 2)
        self.colours = np.asarray([[c["colour"] or (1.0, 1.0, 1.0, 1.0) for c in t["corners"]] for t in triangles], dtype=np.float32).reshape(-1, 3, 4)
        self.corners = self.positions[self.triangles] if len(self.triangles) else np.zeros((0, 3, 3))
        if len(self.positions):
            self.lo, self.hi = self.positions.min(axis=0), self.positions.max(axis=0)
        else:
            self.lo = self.hi = np.zeros(3)

    @property
    def triangle_count(self) -> int:
        return len(self.triangles)


class MeshCache:
    """LRU of decoded meshes bounded by total triangle count."""

    def __init__(self, library, limit: int = MESH_CACHE_TRIANGLES):
        self.library = library
        self.limit = limit
        self.entries: OrderedDict[str, MeshData] = OrderedDict()
        self.triangles = 0
        self.loads = 0
        self.evictions = 0

    def get(self, model_id: str) -> MeshData:
        if model_id in self.entries:
            self.entries.move_to_end(model_id)
            return self.entries[model_id]
        data = MeshData(model_id, self.library.model(model_id).mesh)
        self.loads += 1
        self.entries[model_id] = data
        self.triangles += data.triangle_count
        while self.triangles > self.limit and len(self.entries) > 1:
            _, old = self.entries.popitem(last=False)
            self.triangles -= old.triangle_count
            self.evictions += 1
        return data

    def clear(self) -> None:
        self.entries.clear()
        self.triangles = 0


def record_box(record: dict, family: str, mesh: MeshData | None) -> tuple[np.ndarray, np.ndarray]:
    """Local-space pick box of a record."""

    if family == "instances":
        return mesh.lo, mesh.hi
    if family == "markers":
        return np.array([-MARKER_HALF, 0.0, -MARKER_HALF]), np.array([MARKER_HALF, 4 * MARKER_HALF, MARKER_HALF])
    lo, hi = list(record["min"]), list(record["max"])
    if lo[1] is None or hi[1] is None:
        lo[1], hi[1] = 0.0, PLANAR_DISPLAY_HEIGHT
    return np.asarray(lo, float), np.asarray(hi, float)


class EditorScene:
    def __init__(self, library, level_id: str, mesh_cache: MeshCache | None = None, base: dict | None = None):
        """``base`` supplies a scene directly (blank Studio maps); otherwise it is read from the catalog."""

        self.library = library
        self.level_id = level_id
        self.base = base if base is not None else library.scene(level_id)
        self.meshes = mesh_cache or MeshCache(library)
        self.base_index = {r["id"]: (family, r) for family in FAMILIES for r in self.base.get(family, [])}
        self.view: dict[str, tuple[str, dict]] = dict(self.base_index)
        self.order: list[str] = [r["id"] for family in FAMILIES for r in self.base.get(family, [])]
        self.user: dict[str, tuple[str, dict]] = {}
        self.navigation = copy.deepcopy(self.base.get("navigation"))
        self.minimap_override = None
        self._pick_cache = None
        self.version = 0

    # -- records -------------------------------------------------------------
    def record(self, record_id: str) -> tuple[str, dict] | None:
        return self.view.get(record_id)

    def records(self, family: str | None = None):
        for record_id in self.order:
            entry = self.view.get(record_id)
            if entry and (family is None or entry[0] == family):
                yield entry

    @property
    def bounds(self) -> dict | None:
        return (self.base.get("bounds") or {}).get("map")

    def refresh(self, overlay: overlay_module.Overlay, affected: set[str] | None = None) -> None:
        """Recompute the view for ``affected`` record IDs (None = every record).

        Mirrors overlay.apply: added records are copies of the edit payload,
        a delete removes only user-created records (on a source record it is
        recorded but has no effect), every other targeted edit goes through
        overlay.apply_to_record in edit order.
        """

        added: dict[str, tuple[str, dict, int, str]] = {}
        deleted: set[str] = set()
        by_target: dict[str, list[tuple[int, dict]]] = {}
        nav_edits, minimap = [], None
        family_of = {"add_marker": ("markers", "marker"), "add_collision": ("collision", "collision"), "add_instance": ("instances", "instance")}
        for number, edit in enumerate(overlay.edits):
            op = edit["op"]
            if op in family_of:
                family, key = family_of[op]
                added[edit[key]["id"]] = (family, edit[key], number, op)
            elif op == "set_nav_link":
                nav_edits.append((number, edit))
            elif op == "set_minimap":
                minimap = edit["minimap"]
            elif op == "delete" and edit["target"] in added and edit["target"] not in self.base_index:
                deleted.add(edit["target"])
            else:
                by_target.setdefault(edit["target"], []).append((number, edit))
        targets = set(self.base_index) | set(added) | set(self.user) if affected is None else set(affected)
        for record_id in sorted(targets):
            if record_id in added and record_id not in deleted:
                family, payload, number, op = added[record_id]
                record = copy.deepcopy(payload)
                if family == "collision":
                    record.setdefault("source", {})["user_created"] = True
                record.setdefault("overlay", {"edits": []})["edits"].append({"edit": number, "op": op})
            elif record_id in self.base_index:
                family, record = self.base_index[record_id]
                if record_id in by_target:
                    record = copy.deepcopy(record)
            else:
                self.view.pop(record_id, None)
                self.user.pop(record_id, None)
                if record_id in self.order:
                    self.order.remove(record_id)
                continue
            for number, edit in by_target.get(record_id, []):
                overlay_module.apply_to_record(record, family, edit, number)
            self.view[record_id] = (family, record)
            if record_id in added:
                self.user[record_id] = (family, record)
            if record_id not in self.order:
                self.order.append(record_id)
        self.navigation = copy.deepcopy(self.base.get("navigation"))
        links = (self.navigation or {}).get("links", [])
        for number, edit in nav_edits:
            if 0 <= edit["link"] < len(links):
                links[edit["link"]]["targets"] = list(edit["targets"])
                links[edit["link"]].setdefault("overlay", {"edits": []})["edits"].append({"edit": number, "op": "set_nav_link"})
        self.minimap_override = minimap
        self._pick_cache = None
        self.version += 1

    def edited_scene(self, overlay: overlay_module.Overlay) -> dict:
        """The full overlay.apply result (used for validation and export)."""

        return overlay_module.apply(self.base, overlay)

    # -- picking ---------------------------------------------------------------
    def _pick_arrays(self, layers: set[str]):
        key = (self.version, tuple(sorted(layers)))
        if self._pick_cache and self._pick_cache[0] == key:
            return self._pick_cache[1]
        ids, families, centres, radii = [], [], [], []
        for record_id in self.order:
            entry = self.view.get(record_id)
            if not entry or entry[0] not in layers:
                continue
            family, record = entry
            mesh = self.meshes.get(record["model"]) if family == "instances" else None
            if family == "instances" and mesh.triangle_count == 0:
                continue
            lo, hi = record_box(record, family, mesh)
            scale = np.asarray(record.get("scale") or (1.0, 1.0, 1.0), float)
            lo, hi = lo * scale, hi * scale
            lo, hi = np.minimum(lo, hi), np.maximum(lo, hi)
            centre = (lo + hi) / 2
            radius = float(np.linalg.norm(hi - lo)) / 2
            world_centre = g.yaw_matrix3(record["yaw"]) @ centre + np.asarray(record["translation"], float)
            ids.append(record_id)
            families.append(family)
            centres.append(world_centre)
            radii.append(radius)
        arrays = (ids, families, np.asarray(centres).reshape(-1, 3), np.asarray(radii))
        self._pick_cache = (key, arrays)
        return arrays

    def pick(self, ray: g.Ray, layers: set[str] = frozenset(FAMILIES), limit: int | None = None) -> list[Hit]:
        """All hits along the ray, nearest first, with deterministic tie-breaking."""

        ids, families, centres, radii = self._pick_arrays(set(layers))
        if not ids:
            return []
        # Bounding-sphere prefilter (vectorised): distance from the ray to each centre.
        offset = centres - ray.origin
        along = offset @ ray.direction
        perpendicular = np.einsum("ij,ij->i", offset, offset) - along * along
        candidates = np.where((perpendicular <= radii * radii) & (along + radii >= 0))[0]
        order = candidates[np.argsort(np.maximum(along[candidates] - radii[candidates], 0.0), kind="stable")]
        hits: list[Hit] = []
        best = math.inf
        for index in order:
            entry_distance = max(float(along[index] - radii[index]), 0.0)
            if limit and len(hits) >= limit and entry_distance > best:
                break
            record_id = ids[index]
            family, record = self.view[record_id]
            hit = self._hit_record(ray, record_id, family, record)
            if hit is not None:
                hits.append(hit)
                best = min(best, hit.distance)
        hits.sort(key=Hit.key)
        return hits[:limit] if limit else hits

    def _hit_record(self, ray: g.Ray, record_id: str, family: str, record: dict) -> Hit | None:
        translation, yaw = np.asarray(record["translation"], float), record["yaw"]
        if family == "instances":
            mesh = self.meshes.get(record["model"])
            rotation = g.yaw_matrix3(yaw).T
            scale = np.asarray(record.get("scale") or (1.0, 1.0, 1.0), float)
            # Unnormalised local direction keeps the ray parameter equal to world distance.
            local = g.Ray(rotation @ (ray.origin - translation) / scale, rotation @ ray.direction / scale)
            if g.ray_aabb(local, mesh.lo, mesh.hi) is None:
                return None
            result = g.ray_triangles(local, mesh.corners[:, 0], mesh.corners[:, 1], mesh.corners[:, 2])
            if result is None:
                return None
            distance = result[0]
        else:
            lo, hi = record_box(record, family, None)
            distance = g.ray_box(ray, translation, yaw, lo, hi)
            if distance is None:
                return None
        point = ray.origin + distance * ray.direction
        return Hit(record_id, family, float(distance), tuple(float(v) for v in point))

    def surface_point(self, ray: g.Ray, exclude: set[str] = frozenset()) -> tuple[np.ndarray, str | None] | None:
        """Placement point: nearest geometry hit, else the y = 0 plane."""

        for hit in self.pick(ray, {"instances"}):
            if hit.id not in exclude:
                return np.asarray(hit.point), hit.id
        point = g.ray_plane_y(ray, 0.0)
        return (point, None) if point is not None else None

    def world_bounds(self) -> tuple[np.ndarray, np.ndarray]:
        bounds = self.bounds
        points = [np.asarray(r["translation"], float) for _, r in self.records()]
        if bounds:
            lo = np.array([bounds["x"][0], 0.0, bounds["z"][0]])
            hi = np.array([bounds["x"][1], 0.0, bounds["z"][1]])
            return lo, hi
        if not points:
            return np.zeros(3), np.ones(3)
        stack = np.vstack(points)
        return stack.min(axis=0), stack.max(axis=0) + 1

    def record_world_box(self, record_id: str) -> tuple[np.ndarray, np.ndarray]:
        family, record = self.view[record_id]
        mesh = self.meshes.get(record["model"]) if family == "instances" else None
        lo, hi = record_box(record, family, mesh)
        scale = np.asarray(record.get("scale") or (1.0, 1.0, 1.0), float)
        lo, hi = lo * scale, hi * scale
        corners = np.array([[x, y, z] for x in (lo[0], hi[0]) for y in (lo[1], hi[1]) for z in (lo[2], hi[2])])
        world = corners @ g.yaw_matrix3(record["yaw"]).T + np.asarray(record["translation"], float)
        return world.min(axis=0), world.max(axis=0)
