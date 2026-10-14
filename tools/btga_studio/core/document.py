"""Editing document: overlay edits as undoable transactions, selection and transforms.

Every user action becomes one ``Transaction``: a group of overlay edits
appended together.  Undo truncates the overlay by that group; redo
re-appends the identical edit dicts (same user IDs).  A gizmo drag
previews transforms on the view only and commits one move edit per
object when released, so one drag is one undo step; cancelling restores
the records from the overlay exactly.
"""

from __future__ import annotations

import copy
import math
from dataclasses import dataclass, field

import numpy as np

from tools.btga_assets import overlay as overlay_module
from tools.btga_assets.semantics import field_errors

from . import geometry as g
from .scene import EditorScene

READ_ONLY_CATEGORIES = {"untraced_handler", "ignored_by_dispatcher", "inactive_in_marker_group"}


class EditError(ValueError):
    """An edit was refused before it reached the overlay."""


@dataclass
class Transaction:
    label: str
    edits: list[dict]
    user_index_before: int
    user_index_after: int
    affected: set[str]


@dataclass
class Placement:
    translation: tuple[float, float, float]
    surface: str | None
    warnings: list[str] = field(default_factory=list)


def is_read_only(record: dict) -> str | None:
    """Why a record may not be edited, or None."""

    if record.get("confidence") == "unresolved" or record.get("category") in READ_ONLY_CATEGORIES:
        return "its fields and role are not decoded; edits could not be written back safely"
    return None


def field_editable(spec: dict) -> str | None:
    if spec.get("evidence") == "unresolved":
        return "the field's meaning is unresolved"
    return None


class EditorDocument:
    def __init__(self, scene: EditorScene, overlay: overlay_module.Overlay | None = None):
        self.scene = scene
        self.overlay = overlay or overlay_module.Overlay.for_scene(scene.base)
        self.undo_stack: list[Transaction] = []
        self.redo_stack: list[Transaction] = []
        self.selection: list[str] = []
        self.hidden: set[str] = set()
        self.locked: set[str] = set()
        self.saved_token = 0
        self._token = 0
        self._tokens: list[int] = []
        self._transform = None
        self.scene.refresh(self.overlay)

    # -- state ----------------------------------------------------------------
    @property
    def dirty(self) -> bool:
        return self.current_token != self.saved_token

    @property
    def current_token(self) -> int:
        return self._tokens[-1] if self._tokens else 0

    def mark_saved(self) -> None:
        self.saved_token = self.current_token

    def record(self, record_id: str) -> dict:
        entry = self.scene.record(record_id)
        if entry is None:
            raise EditError(f"no record {record_id!r}")
        return entry[1]

    def family(self, record_id: str) -> str:
        return self.scene.record(record_id)[0]

    # -- transactions -------------------------------------------------------------
    def _commit(self, label: str, edits: list[dict], affected: set[str]) -> Transaction:
        if self._transform is not None:
            raise EditError("finish or cancel the active transform first")
        before = self.overlay.next_user_index
        stored = [self.overlay.add(edit) for edit in edits]
        for edit in stored:
            for key in ("marker", "collision", "instance"):
                if key in edit and edit["op"].startswith("add_"):
                    affected.add(edit[key]["id"])
        transaction = Transaction(label, stored, before, self.overlay.next_user_index, affected)
        self.undo_stack.append(transaction)
        self.redo_stack.clear()
        self._token += 1
        self._tokens.append(self._token)
        self.scene.refresh(self.overlay, affected)
        return transaction

    def undo(self) -> Transaction | None:
        if self._transform is not None:
            self.cancel_transform()
            return None
        if not self.undo_stack:
            return None
        transaction = self.undo_stack.pop()
        self.overlay.revert(len(transaction.edits))
        self.overlay.next_user_index = transaction.user_index_before
        self.redo_stack.append(transaction)
        token = self._tokens.pop()
        transaction.token = token
        self.scene.refresh(self.overlay, set(transaction.affected))
        self.selection = [s for s in self.selection if self.scene.record(s)]
        return transaction

    def redo(self) -> Transaction | None:
        if not self.redo_stack or self._transform is not None:
            return None
        transaction = self.redo_stack.pop()
        self.overlay.edits.extend(copy.deepcopy(transaction.edits))
        self.overlay.next_user_index = transaction.user_index_after
        self.undo_stack.append(transaction)
        self._tokens.append(transaction.token)
        self.scene.refresh(self.overlay, set(transaction.affected))
        return transaction

    # -- selection ------------------------------------------------------------------
    def select(self, record_id: str | None, toggle: bool = False) -> None:
        if record_id is not None and (record_id in self.locked or record_id in self.hidden):
            return
        if record_id is None:
            if not toggle:
                self.selection = []
            return
        if toggle:
            if record_id in self.selection:
                self.selection.remove(record_id)
            else:
                self.selection.append(record_id)
        else:
            self.selection = [record_id]

    def _check_editable(self, ids) -> None:
        for record_id in ids:
            if record_id in self.locked:
                raise EditError(f"{record_id} is locked")
            reason = is_read_only(self.record(record_id))
            if reason:
                raise EditError(f"{record_id} is read-only: {reason}")

    # -- placement --------------------------------------------------------------------
    def placement(self, point, grid: float = 0.0, template: dict | None = None) -> Placement:
        x, y, z = (g.snap(float(point[0]), grid), float(point[1]), g.snap(float(point[2]), grid))
        warnings = []
        bounds = self.scene.bounds
        if bounds and not (bounds["x"][0] <= x <= bounds["x"][1] and bounds["z"][0] <= z <= bounds["z"][1]):
            raise EditError(f"({x:.0f}, {z:.0f}) is outside the map limits x {bounds['x']} z {bounds['z']}")
        if not bounds:
            warnings.append("this map has no proven limits; placement is unchecked")
        if template is not None and template.get("collision_status", "none_stored") != "proven":
            warnings.append("collision for this object is unknown or planar on this platform")
        return Placement((x, y, z), None, warnings)

    def place_marker(self, template: dict, point, yaw: int = 0, grid: float = 0.0, values: dict | None = None) -> str:
        if is_read_only(template):
            raise EditError("unresolved records cannot be placed")
        where = self.placement(point, grid)
        marker = copy.deepcopy(template)
        for key in ("id", "overlay", "disabled"):
            marker.pop(key, None)
        marker.update({"translation": list(where.translation), "yaw": int(yaw) & 0xFFFF,
                       "source": {"user_created": True, "template": template.get("id")}})
        for name, value in (values or {}).items():
            self._validate_field(marker, name, value)
            marker["fields"][name]["value"] = value
        transaction = self._commit(f"Place {marker.get('category')}", [{"op": "add_marker", "marker": marker}], set())
        new_id = transaction.edits[0]["marker"]["id"]
        self.selection = [new_id]
        return new_id

    def place_model(self, model_id: str, point, yaw: int = 0, grid: float = 0.0) -> str:
        if model_id not in self.scene.library._models:
            raise EditError(f"model {model_id} is not in the catalog")
        where = self.placement(point, grid)
        instance = {"model": model_id, "translation": list(where.translation), "yaw": int(yaw) & 0xFFFF, "category": "model",
                    "source": {"user_created": True}}
        transaction = self._commit("Place model", [{"op": "add_instance", "instance": instance}], set())
        new_id = transaction.edits[0]["instance"]["id"]
        self.selection = [new_id]
        return new_id

    # -- transforms ---------------------------------------------------------------------
    def set_transform(self, record_id: str, translation=None, yaw: int | None = None) -> None:
        """Numeric transform edit (one transaction)."""

        self._check_editable([record_id])
        edit = {"op": "move", "target": record_id}
        if translation is not None:
            values = [float(v) for v in translation]
            if not all(math.isfinite(v) for v in values):
                raise EditError("position must be finite")
            self._check_bounds(values)
            edit["translation"] = values
        if yaw is not None:
            edit["yaw"] = int(yaw) & 0xFFFF
        self._commit("Move", [edit], {record_id})

    def _check_bounds(self, translation) -> None:
        bounds = self.scene.bounds
        if bounds and not (bounds["x"][0] <= translation[0] <= bounds["x"][1] and bounds["z"][0] <= translation[2] <= bounds["z"][1]):
            raise EditError(f"({translation[0]:.0f}, {translation[2]:.0f}) is outside the map limits")

    def begin_transform(self, ids: list[str] | None = None) -> None:
        ids = list(ids if ids is not None else self.selection)
        if not ids:
            raise EditError("nothing selected")
        self._check_editable(ids)
        originals = {i: (list(self.record(i)["translation"]), self.record(i)["yaw"]) for i in ids}
        scales = {i: list(self.record(i).get("scale") or (1.0, 1.0, 1.0)) for i in ids}
        pivot = np.mean([o[0] for o in originals.values()], axis=0)
        self._transform = {"ids": ids, "originals": originals, "pivot": pivot, "delta": np.zeros(3), "yaw": 0, "scales": scales,
                           "scale": np.ones(3)}

    @property
    def transforming(self) -> bool:
        return self._transform is not None

    def update_transform(self, delta=None, yaw_delta: int | None = None, scale=None) -> None:
        """Preview: translate all by ``delta``, rotate about their pivot by ``yaw_delta`` units and
        multiply placed objects' scale by ``scale`` (instances only)."""

        state = self._transform
        if state is None:
            raise EditError("no active transform")
        if delta is not None:
            state["delta"] = np.asarray(delta, float)
        if yaw_delta is not None:
            state["yaw"] = int(yaw_delta)
        if scale is not None:
            factor = np.asarray(scale, float)
            if not np.all(np.isfinite(factor)) or np.any(factor <= 0.01) or np.any(factor > 100):
                raise EditError("scale must be between 0.01 and 100")
            state["scale"] = factor
        for record_id, (translation, yaw) in state["originals"].items():
            new_translation, new_yaw = self._transformed(state, translation, yaw)
            record = self.record(record_id)
            if record is self.scene.base_index.get(record_id, (None, None))[1]:
                record = copy.deepcopy(record)
                self.scene.view[record_id] = (self.family(record_id), record)
            record["translation"] = new_translation
            record["yaw"] = new_yaw
            if self.family(record_id) == "instances" and not np.allclose(state["scale"], 1.0):
                record["scale"] = self._scaled(state, record_id)
        self.scene._pick_cache = None

    @staticmethod
    def _scaled(state, record_id) -> list[float]:
        return [round(float(a * b), 6) for a, b in zip(state["scales"][record_id], state["scale"])]

    @staticmethod
    def _transformed(state, translation, yaw):
        rotation = g.yaw_matrix3(state["yaw"])
        relative = np.asarray(translation, float) - state["pivot"]
        moved = rotation @ relative + state["pivot"] + state["delta"]
        return [float(v) for v in moved], (int(yaw) + state["yaw"]) & 0xFFFF

    def commit_transform(self, label: str = "Transform") -> Transaction | None:
        state, self._transform = self._transform, None
        if state is None:
            return None
        edits = []
        for record_id, (translation, yaw) in state["originals"].items():
            new_translation, new_yaw = self._transformed(state, translation, yaw)
            scaled = self.family(record_id) == "instances" and not np.allclose(state["scale"], 1.0)
            if new_translation == [float(v) for v in translation] and new_yaw == yaw and not scaled:
                continue
            try:
                self._check_bounds(new_translation)
            except EditError:
                self.scene.refresh(self.overlay, set(state["originals"]))
                raise
            edit = {"op": "move", "target": record_id, "translation": new_translation, "yaw": new_yaw}
            if scaled:
                edit["scale"] = self._scaled(state, record_id)
            edits.append(edit)
        if not edits:
            self.scene.refresh(self.overlay, set(state["originals"]))
            return None
        return self._commit(label, edits, set(state["originals"]))

    def cancel_transform(self) -> None:
        state, self._transform = self._transform, None
        if state is not None:
            self.scene.refresh(self.overlay, set(state["originals"]))

    # -- other edits ------------------------------------------------------------------------
    def duplicate(self, ids: list[str] | None = None, offset=(64.0, 0.0, 64.0)) -> list[str]:
        ids = list(ids if ids is not None else self.selection)
        self._check_editable(ids)
        edits = []
        for record_id in ids:
            family, record = self.scene.record(record_id)
            copy_ = copy.deepcopy(record)
            for key in ("id", "overlay", "disabled"):
                copy_.pop(key, None)
            copy_["translation"] = [a + b for a, b in zip(record["translation"], offset)]
            self._check_bounds(copy_["translation"])
            copy_["source"] = {"user_created": True, "template": record_id}
            if family == "instances":
                edits.append({"op": "add_instance", "instance": {k: copy_[k] for k in ("model", "translation", "yaw", "category", "source")}})
            elif family == "markers":
                edits.append({"op": "add_marker", "marker": copy_})
            else:
                edits.append({"op": "add_collision", "collision": copy_})
        transaction = self._commit("Duplicate", edits, set())
        new_ids = [e[{"add_instance": "instance", "add_marker": "marker", "add_collision": "collision"}[e["op"]]]["id"] for e in transaction.edits]
        self.selection = new_ids
        return new_ids

    def delete(self, ids: list[str] | None = None) -> None:
        """User-created records are deleted; source records are disabled (non-destructive)."""

        ids = list(ids if ids is not None else self.selection)
        self._check_editable(ids)
        edits = []
        for record_id in ids:
            record = self.record(record_id)
            edits.append({"op": "delete" if record.get("source", {}).get("user_created") else "disable", "target": record_id})
        self._commit("Delete", edits, set(ids))
        self.selection = [s for s in self.selection if self.scene.record(s) and not self.record(s).get("disabled")]

    # -- clipboard and view state -------------------------------------------------------
    CLIPBOARD = "btga-studio-clipboard"

    def copy(self, ids: list[str] | None = None) -> str:
        """Deterministic clipboard text for the selected records."""

        import json

        ids = sorted(ids if ids is not None else self.selection)
        records = []
        for record_id in ids:
            family, record = self.scene.record(record_id)
            if is_read_only(record):
                raise EditError(f"{record_id} is read-only and cannot be copied")
            payload = copy.deepcopy(record)
            for key in ("overlay", "disabled"):
                payload.pop(key, None)
            records.append({"family": family, "record": payload})
        return json.dumps({"format": self.CLIPBOARD, "version": 1, "records": records}, sort_keys=True)

    def paste(self, text: str, point=None) -> list[str]:
        """Add copies of clipboard records; with ``point`` the group centre lands there."""

        import json

        try:
            data = json.loads(text)
        except ValueError as error:
            raise EditError(f"clipboard does not hold Studio records: {error}") from error
        if data.get("format") != self.CLIPBOARD or data.get("version") != 1:
            raise EditError("clipboard does not hold Studio records")
        entries = data["records"]
        if not entries:
            return []
        centre = np.mean([e["record"]["translation"] for e in entries], axis=0)
        offset = (np.asarray(point, float) - centre) if point is not None else np.array([64.0, 0.0, 64.0])
        if point is not None:
            offset[1] = 0.0
        edits = []
        for entry in entries:
            record = copy.deepcopy(entry["record"])
            source_id = record.pop("id", None)
            record["translation"] = [float(a + b) for a, b in zip(record["translation"], offset)]
            self._check_bounds(record["translation"])
            record["source"] = {"user_created": True, "template": source_id}
            family = entry["family"]
            if family == "instances":
                keep = {k: record[k] for k in ("model", "translation", "yaw", "category", "source", "scale") if k in record}
                edits.append({"op": "add_instance", "instance": keep})
            elif family == "markers":
                edits.append({"op": "add_marker", "marker": record})
            elif family == "collision":
                edits.append({"op": "add_collision", "collision": record})
            else:
                raise EditError(f"unknown record family {family!r}")
        transaction = self._commit("Paste", edits, set())
        new_ids = [e[{"add_instance": "instance", "add_marker": "marker", "add_collision": "collision"}[e["op"]]]["id"] for e in transaction.edits]
        self.selection = new_ids
        return new_ids

    def hide(self, ids: list[str] | None = None) -> None:
        ids = list(ids if ids is not None else self.selection)
        self.hidden |= set(ids)
        self.selection = [s for s in self.selection if s not in self.hidden]

    def isolate(self, ids: list[str] | None = None) -> None:
        keep = set(ids if ids is not None else self.selection)
        self.hidden = {record_id for record_id in self.scene.view if record_id not in keep}

    def unhide_all(self) -> None:
        self.hidden = set()

    def lock(self, ids: list[str] | None = None) -> None:
        ids = list(ids if ids is not None else self.selection)
        self.locked |= set(ids)
        self.selection = [s for s in self.selection if s not in self.locked]

    def unlock_all(self) -> None:
        self.locked = set()

    def enable(self, record_id: str) -> None:
        self._commit("Enable", [{"op": "enable", "target": record_id}], {record_id})

    def _validate_field(self, record: dict, name: str, value) -> None:
        reason = is_read_only(record)
        if reason:
            raise EditError(f"record is read-only: {reason}")
        spec = record.get("fields", {}).get(name)
        if spec is None:
            raise EditError(f"no field {name!r}")
        reason = field_editable(spec)
        if reason:
            raise EditError(f"{name} cannot be edited: {reason}")
        problems = field_errors(name, spec, value)
        if problems:
            raise EditError("; ".join(problems))

    def set_field(self, record_id: str, name: str, value) -> None:
        self._validate_field(self.record(record_id), name, value)
        if self.record(record_id)["fields"][name]["value"] == value:
            return
        self._commit(f"Set {name}", [{"op": "set_field", "target": record_id, "field": name, "value": value}], {record_id})

    def set_collision(self, record_id: str, lo, hi) -> None:
        record = self.record(record_id)
        if self.family(record_id) != "collision":
            raise EditError("not a collision shape")
        planar = record["vertical"]["status"] != "proven"
        lo, hi = list(lo), list(hi)
        if planar:
            lo[1] = hi[1] = None
        if lo[0] >= hi[0] or lo[2] >= hi[2] or (not planar and lo[1] > hi[1]):
            raise EditError("collision extent is empty or inverted")
        self._commit("Edit collision", [{"op": "set_collision", "target": record_id, "min": lo, "max": hi}], {record_id})

    def set_minimap(self, minimap: dict) -> None:
        self._commit("Minimap", [{"op": "set_minimap", "minimap": copy.deepcopy(minimap)}], set())

    def set_nav_link(self, link: int, targets: list[int]) -> None:
        count = (self.scene.navigation or {}).get("point_count", 0)
        if any(not isinstance(t, int) or not 0 <= t < count for t in targets):
            raise EditError(f"navigation targets must be point indices below {count}")
        self._commit("Navigation link", [{"op": "set_nav_link", "link": link, "targets": list(targets)}], set())
