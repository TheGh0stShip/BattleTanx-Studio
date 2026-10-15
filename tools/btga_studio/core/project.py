"""BattleTanx Studio project files (``btga-studio-project`` v2).

A project is human-readable, sorted JSON.  It references source content only
by content-derived IDs and source hashes, never by path, and embeds no game
data:

    {"schema": "btga-studio-project", "schema_version": 2,
     "project": {id, title, description, target: {game, package}},
     "dependencies": {sources: [...], starter: N},
     "maps": {key: {kind: "imported" | "blank", title, scene, source_fingerprint?,
                    base? (blank maps: bounds, environment), overlay, history, view, minimap}},
     "campaigns": [...], "missions": {...}, "multiplayer": {...}, "minigames": {...},
     "tanks": {...}, "gangs": {...}, "extra": {...}}

Version 1 files (one scene plus overlay) migrate to one imported map
``main``.  Newer versions are refused.  Unknown keys at project and map
level are carried through unchanged.  Undo and redo history is saved per
map, so it survives save and reload.  Opening an imported map whose source
fingerprint differs raises ``ProjectMismatch``.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
import secrets
import time
from dataclasses import dataclass, field
from pathlib import Path

from tools.btga_assets import overlay as overlay_module

from .atomic import atomic_write_text
from .document import EditorDocument, Transaction
from .history import DefinitionHistory, observe
from .scene import EditorScene

PROJECT_SCHEMA = "btga-studio-project"
PROJECT_VERSION = 2
SUPPORTED_VERSIONS = (1, 2)
TARGET_GAMES = ("studio", "n64_btga", "n64_bt1", "ps1")
KEY = re.compile(r"^[a-z0-9][a-z0-9_]{0,47}$")
KNOWN_TOP = {"schema", "schema_version", "project", "dependencies", "maps", "campaigns", "missions", "multiplayer", "minigames",
             "tanks", "gangs", "extra", "definition_history"}
KNOWN_MAP = {"kind", "title", "scene", "source_fingerprint", "base", "overlay", "history", "view", "minimap", "extra"}
DEFINITION_FAMILIES = ("missions", "multiplayer", "minigames", "tanks", "gangs")
MAX_TEXT = 200


class ProjectError(ValueError):
    pass


class ProjectMismatch(ProjectError):
    """The project's recorded source differs from (or is missing in) the loaded catalogs."""


class FutureVersion(ProjectError):
    """The file was written by a newer Studio."""


def scene_fingerprint(scene: dict) -> str:
    identity = {"id": scene["id"], "source": scene["source"]}
    text = json.dumps(identity, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(text.encode()).hexdigest()


def check_key(key: str, what: str) -> str:
    if not isinstance(key, str) or not KEY.match(key):
        raise ProjectError(f"{what} {key!r} must be 1-48 lowercase letters, digits or underscores")
    return key


def _text(value, what: str) -> str:
    if not isinstance(value, str) or len(value) > MAX_TEXT or any(ord(c) < 32 for c in value):
        raise ProjectError(f"{what} must be printable text of at most {MAX_TEXT} characters")
    return value


def blank_base(key: str, title: str, half_extent: float = 2048.0, colour=(96, 120, 150)) -> dict:
    """The base scene of a blank Studio map: no records, only limits and environment."""

    check_key(key, "map key")
    return {
        "id": f"studio:scene:map_{key}", "schema": "btga-neutral", "schema_version": 1, "kind": "scene",
        "source": {"platform": "studio", "blank": True, "map": key},
        "semantics_version": 3, "instances": [], "markers": [], "collision": [],
        "environment": [{"kind": "studio", "clear_and_fog_colour": list(colour), "evidence": "Studio environment"}],
        "bounds": {"map": {"x": [-half_extent, half_extent], "z": [-half_extent, half_extent], "status": "studio",
                           "evidence": "Studio map limits"}},
        "minimap": {"status": "generated", "selections": []}, "navigation": None, "music": {"status": "none"},
        "unresolved": [], "models": [], "title": title,
    }


# ------------------------------------------------------------------ history --

def history_document(document: EditorDocument) -> dict:
    """Undo/redo stacks as data.  Undo entries reference the overlay tail; redo entries carry their edits."""

    undo = [{"label": t.label, "edits": len(t.edits), "user_index_before": t.user_index_before,
             "user_index_after": t.user_index_after, "affected": sorted(t.affected), "sequence": t.sequence} for t in document.undo_stack]
    redo = [{"label": t.label, "edits": copy.deepcopy(t.edits), "user_index_before": t.user_index_before,
             "user_index_after": t.user_index_after, "affected": sorted(t.affected), "sequence": t.sequence} for t in document.redo_stack]
    return {"undo": undo, "redo": redo}


def _check_history(history, what: str) -> None:
    """Shape check so a damaged history fails as ProjectError instead of crashing the editor."""

    if history is None:
        return
    if not isinstance(history, dict):
        raise ProjectError(f"{what}: history must be an object")
    for stack in ("undo", "redo"):
        entries = history.get(stack, [])
        if not isinstance(entries, list):
            raise ProjectError(f"{what}: history.{stack} must be a list")
        for position, entry in enumerate(entries):
            where = f"{what}: history.{stack}[{position}]"
            if not isinstance(entry, dict):
                raise ProjectError(f"{where} must be an object")
            for name in ("user_index_before", "user_index_after"):
                if not _plain_int(entry.get(name)):
                    raise ProjectError(f"{where}.{name} must be an integer")
            if not isinstance(entry.get("affected", []), list) or not isinstance(entry.get("label", ""), str):
                raise ProjectError(f"{where} has a malformed label or affected list")
            edits = entry.get("edits")
            if stack == "undo" and not (_plain_int(edits) and edits >= 0):
                raise ProjectError(f"{where}.edits must be a non-negative integer")
            if stack == "redo" and not (isinstance(edits, list) and all(isinstance(e, dict) for e in edits)):
                raise ProjectError(f"{where}.edits must be a list of edits")


def _plain_int(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _sequence(entry: dict) -> int:
    value = entry.get("sequence")
    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
        observe(value)
        return value
    return 0  # histories saved before sequences existed sort before anything new


def restore_history(document: EditorDocument, history: dict | None) -> None:
    if not history:
        return
    _check_history(history, "history")
    edits = document.overlay.edits
    total = sum(entry["edits"] for entry in history.get("undo", []))
    if total > len(edits):
        raise ProjectError("saved undo history refers to more edits than the overlay holds")
    position = len(edits) - total
    stack = []
    for entry in history.get("undo", []):
        count = entry["edits"]
        stack.append(Transaction(entry["label"], edits[position:position + count], entry["user_index_before"],
                                 entry["user_index_after"], set(entry["affected"]), _sequence(entry)))
        position += count
    redo = [Transaction(entry["label"], copy.deepcopy(entry["edits"]), entry["user_index_before"], entry["user_index_after"],
                        set(entry["affected"]), _sequence(entry)) for entry in history.get("redo", [])]
    document.undo_stack = stack
    document.redo_stack = redo
    document._tokens = list(range(1, len(stack) + 1))
    document._token = len(stack) + len(redo)
    for offset, transaction in enumerate(reversed(redo)):
        transaction.token = len(stack) + offset + 1


# --------------------------------------------------------------------- maps --

@dataclass
class MapEntry:
    key: str
    kind: str  # imported | blank
    title: str
    scene: str
    source_fingerprint: str | None = None
    base: dict | None = None
    overlay: dict | None = None
    history: dict | None = None
    view: dict = field(default_factory=lambda: {"hidden": [], "locked": []})
    minimap: dict = field(default_factory=lambda: {"mode": "source"})
    extra: dict = field(default_factory=dict)
    document: EditorDocument | None = None

    def sync(self) -> None:
        """Copy the live document state back into the serialisable fields."""

        if self.document is None:
            return
        self.overlay = json.loads(self.document.overlay.to_json())
        self.history = history_document(self.document)
        self.view = {"hidden": sorted(self.document.hidden), "locked": sorted(self.document.locked)}

    def as_dict(self) -> dict:
        self.sync()
        out = {"kind": self.kind, "title": self.title, "scene": self.scene, "overlay": self.overlay,
               "history": copy.deepcopy(self.history) or {"undo": [], "redo": []}, "view": self.view, "minimap": self.minimap}
        if self.source_fingerprint:
            out["source_fingerprint"] = self.source_fingerprint
        if self.base is not None:
            out["base"] = self.base
        if self.extra:
            out["extra"] = self.extra
        return out


class StudioProject:
    def __init__(self, title: str, target_game: str = "studio", project_id: str | None = None):
        if target_game not in TARGET_GAMES:
            raise ProjectError(f"unknown target game {target_game!r}")
        self.project = {"id": project_id or secrets.token_hex(8), "title": _text(title, "title"), "description": "",
                        "target": {"game": target_game, "package": "pc-port"}}
        self.dependencies: dict = {"sources": [], "starter": None}
        self.maps: dict[str, MapEntry] = {}
        self.campaigns: list[dict] = []
        self.missions: dict[str, dict] = {}
        self.multiplayer: dict[str, dict] = {}
        self.minigames: dict[str, dict] = {}
        self.tanks: dict[str, dict] = {}
        self.gangs: dict[str, dict] = {}
        self.extra: dict = {}
        self.unknown: dict = {}
        self.path: Path | None = None
        self.saved_text: str | None = None
        self.definitions = DefinitionHistory(self)

    @property
    def saved_text(self) -> str | None:
        return self._saved_text

    @saved_text.setter
    def saved_text(self, text: str | None) -> None:
        self._saved_text = text
        self._saved_content = _content(json.loads(text)) if text else None

    # -- maps -----------------------------------------------------------------------
    def add_imported_map(self, key: str, library, scene_id: str, title: str | None = None) -> MapEntry:
        check_key(key, "map key")
        if key in self.maps:
            raise ProjectError(f"map {key!r} already exists")
        scene = library.scene(scene_id)
        entry = MapEntry(key, "imported", _text(title or scene.get("title") or scene_id, "title"), scene_id, scene_fingerprint(scene))
        entry.overlay = json.loads(overlay_module.Overlay.for_scene(scene).to_json())
        self.maps[key] = entry
        return entry

    def add_blank_map(self, key: str, title: str, half_extent: float = 2048.0) -> MapEntry:
        check_key(key, "map key")
        if key in self.maps:
            raise ProjectError(f"map {key!r} already exists")
        if not 128 <= half_extent <= 32768:
            raise ProjectError("map half-extent must be between 128 and 32768 units")
        base = blank_base(key, _text(title, "title"), half_extent)
        entry = MapEntry(key, "blank", base["title"], base["id"], None, base)
        entry.overlay = json.loads(overlay_module.Overlay.for_scene(base).to_json())
        entry.minimap = {"mode": "generated", "width": 64, "height": 64}
        self.maps[key] = entry
        return entry

    def remove_map(self, key: str) -> None:
        if key not in self.maps:
            raise ProjectError(f"no map {key!r}")
        del self.maps[key]

    def open_map(self, key: str, library, mesh_cache=None) -> EditorDocument:
        entry = self.maps.get(key)
        if entry is None:
            raise ProjectError(f"no map {key!r}")
        if entry.document is not None:
            return entry.document
        if entry.kind == "blank":
            scene = EditorScene(library, entry.scene, mesh_cache, base=copy.deepcopy(entry.base))
        else:
            if entry.scene not in library._levels:
                raise ProjectMismatch(f"map {key!r} needs scene {entry.scene}, which no loaded catalog provides")
            scene = EditorScene(library, entry.scene, mesh_cache)
            actual = scene_fingerprint(scene.base)
            if actual != entry.source_fingerprint:
                raise ProjectMismatch(f"map {key!r}: scene {entry.scene} has a different source (project "
                                      f"{(entry.source_fingerprint or '')[:12]}, catalog {actual[:12]}); its edits were not applied")
        try:
            overlay = overlay_module.Overlay.from_json(json.dumps(entry.overlay)) if entry.overlay else overlay_module.Overlay.for_scene(scene.base)
        except (KeyError, TypeError, ValueError, AttributeError) as error:
            raise ProjectError(f"map {key!r}: field overlay is malformed ({type(error).__name__}: {error})") from error
        if overlay.scene_id != scene.base["id"]:
            raise ProjectMismatch(f"map {key!r}: overlay targets {overlay.scene_id}")
        document = EditorDocument(scene, overlay)
        restore_history(document, entry.history)
        document.hidden = set(entry.view.get("hidden", []))
        document.locked = set(entry.view.get("locked", []))
        document.mark_saved()
        entry.document = document
        return document

    def close_map(self, key: str) -> None:
        entry = self.maps[key]
        entry.sync()
        entry.document = None

    def set_map_settings(self, key: str, half_extent: float | None = None, colour=None, title: str | None = None) -> None:
        entry = self.maps[key]
        if title is not None:
            entry.title = _text(title, "title")
        if entry.kind != "blank":
            if half_extent is not None or colour is not None:
                raise ProjectError("limits and environment of imported maps come from the source")
            return
        if half_extent is not None:
            if not 128 <= half_extent <= 32768:
                raise ProjectError("map half-extent must be between 128 and 32768 units")
            entry.base["bounds"]["map"]["x"] = [-half_extent, half_extent]
            entry.base["bounds"]["map"]["z"] = [-half_extent, half_extent]
        if colour is not None:
            if len(colour) != 3 or any(not 0 <= int(c) <= 255 for c in colour):
                raise ProjectError("colour needs three components 0-255")
            entry.base["environment"][0]["clear_and_fog_colour"] = [int(c) for c in colour]
        if entry.document is not None:
            entry.document.scene.base["bounds"] = copy.deepcopy(entry.base["bounds"])
            entry.document.scene.base["environment"] = copy.deepcopy(entry.base["environment"])

    def set_map_sky(self, key: str, sky: dict | None) -> None:
        """Set (or with None remove) the sky panorama of a blank map; see ``core.sky``."""

        from . import sky as sky_module

        entry = self.maps[key]
        if entry.kind != "blank":
            raise ProjectError("the sky of an imported map comes from the source")
        environment = entry.base["environment"][0]
        if sky is None:
            environment.pop("sky", None)
        else:
            try:
                environment["sky"] = sky_module.check(sky)
            except sky_module.SkyError as error:
                raise ProjectError(str(error)) from error
        if entry.document is not None:
            entry.document.scene.base["environment"] = copy.deepcopy(entry.base["environment"])

    @property
    def dirty(self) -> bool:
        """Unsaved changes to project content.  Undo history is not content: undoing back to the saved state is clean."""

        if self._saved_content is None:
            return True
        return _content(json.loads(dumps(self))) != self._saved_content

    # -- definitions -------------------------------------------------------------------------
    def define(self, family: str, key: str, data: dict) -> None:
        """Add or replace a mission, multiplayer map, minigame, tank or gang definition."""

        if family not in DEFINITION_FAMILIES:
            raise ProjectError(f"unknown definition family {family!r}")
        check_key(key, family)
        if "title" in data:
            _text(data["title"], "title")
        getattr(self, family)[key] = copy.deepcopy(data)

    def problems(self, library=None) -> list[dict]:
        """Definition and dependency problems, each naming its object and field."""

        out = []
        model_ids = set(library._models) if library is not None else None
        for family in ("missions", "multiplayer", "minigames"):
            for key, data in sorted(getattr(self, family).items()):
                if data.get("map") not in self.maps:
                    out.append({"code": "MISSING_MAP", "target": f"{family}:{key}", "field": "map", "message": f"map {data.get('map')!r} does not exist"})
        for campaign in self.campaigns:
            for mission in campaign.get("missions", []):
                if mission not in self.missions:
                    out.append({"code": "MISSING_MISSION", "target": f"campaigns:{campaign.get('id')}", "field": "missions",
                                "message": f"mission {mission!r} does not exist"})
        for key, data in sorted(self.missions.items()):
            entry = self.maps.get(data.get("map"))
            records = _record_ids(entry) if entry else None
            for objective in data.get("objectives", []):
                if records is not None and objective not in records:
                    out.append({"code": "MISSING_RECORD", "target": f"missions:{key}", "field": "objectives",
                                "message": f"objective record {objective!r} is not in map {entry.key}"})
        for key, data in sorted(self.multiplayer.items()):
            players = data.get("max_players", 4)
            if not isinstance(players, int) or not 2 <= players <= 4:
                out.append({"code": "INVALID_FIELD", "target": f"multiplayer:{key}", "field": "max_players", "message": "max_players must be 2-4"})
        for key, data in sorted(self.tanks.items()):
            if model_ids is not None and data.get("model") not in model_ids:
                out.append({"code": "MISSING_MODEL", "target": f"tanks:{key}", "field": "model", "message": f"model {data.get('model')!r} is not loaded"})
        for key, data in sorted(self.gangs.items()):
            for tank in data.get("tanks", []):
                if tank not in self.tanks:
                    out.append({"code": "MISSING_TANK", "target": f"gangs:{key}", "field": "tanks", "message": f"tank {tank!r} is not defined"})
        if library is not None:
            for entry in self.maps.values():
                if entry.kind == "imported" and entry.scene not in library._levels:
                    out.append({"code": "MISSING_SOURCE", "target": f"maps:{entry.key}", "field": "scene",
                                "message": f"scene {entry.scene} is not in any loaded catalog; register the source it came from"})
                sky = (entry.base or {}).get("environment", [{}])[0].get("sky") if entry.kind == "blank" and entry.base else None
                if sky and sky.get("texture") not in library._textures:
                    out.append({"code": "MISSING_IMPORT", "target": f"maps:{entry.key}", "field": "sky",
                                "message": f"sky picture {sky.get('texture')} is not in your library; import it again or open a package that has it"})
            loaded = {_source_key(s) for s in library.catalog.get("sources", [])}
            for source in self.dependencies.get("sources", []):
                if _source_key(source) not in loaded:
                    out.append({"code": "MISSING_SOURCE", "target": "dependencies", "field": "sources",
                                "message": f"{source.get('platform')} source {_source_key(source)[1][:12]} is not registered"})
        return out

    def compute_dependencies(self, library) -> None:
        """Record the sources imported maps and referenced models come from (hashes only)."""

        sources_by_platform: dict[str, list] = {}
        for source in library.catalog.get("sources", []):
            sources_by_platform.setdefault(source.get("platform"), []).append(source)
        platforms = set()
        for entry in self.maps.values():
            if entry.kind == "imported":
                platforms.add(entry.scene.split(":", 1)[0])
            edits = entry.document.overlay.edits if entry.document is not None else (entry.overlay or {}).get("edits", [])
            for edit in edits:
                if edit["op"] == "add_instance":
                    platforms.add(edit["instance"]["model"].split(":", 1)[0])
        for tank in self.tanks.values():
            if isinstance(tank.get("model"), str):
                platforms.add(tank["model"].split(":", 1)[0])
        needed = {}
        for platform in sorted(platforms - {"studio", "user", "common"}):
            for source in sources_by_platform.get(platform, []):
                needed[_source_key(source)] = {k: source[k] for k in sorted(source) if k in ("platform", "title", "rom_sha1", "rom_sha256", "file_sha256")}
        self.dependencies["sources"] = [needed[k] for k in sorted(needed)]
        starter = library.starter() if hasattr(library, "starter") else None
        uses_starter = "studio" in platforms or any(e.kind == "blank" for e in self.maps.values())
        self.dependencies["starter"] = starter["starter_version"] if starter and uses_starter else None

    # -- serialisation --------------------------------------------------------------------------
    def as_dict(self) -> dict:
        data = {
            "schema": PROJECT_SCHEMA, "schema_version": PROJECT_VERSION, "project": self.project, "dependencies": self.dependencies,
            "maps": {key: entry.as_dict() for key, entry in sorted(self.maps.items())},
            "campaigns": self.campaigns, "missions": self.missions, "multiplayer": self.multiplayer, "minigames": self.minigames,
            "tanks": self.tanks, "gangs": self.gangs,
        }
        if self.extra:
            data["extra"] = self.extra
        history = self.definitions.as_dict()
        if history["undo"] or history["redo"]:
            data["definition_history"] = history
        data.update(self.unknown)
        _rank_sequences(data)
        return data

    @classmethod
    def from_dict(cls, data: dict) -> "StudioProject":
        if not isinstance(data, dict) or data.get("schema") != PROJECT_SCHEMA:
            raise ProjectError("not a BattleTanx Studio project")
        version = data.get("schema_version")
        if not isinstance(version, int) or isinstance(version, bool):
            raise ProjectError("project has no schema version")
        if version > PROJECT_VERSION:
            raise FutureVersion(f"project schema {version} is newer than this Studio supports ({PROJECT_VERSION}); update Studio to open it")
        if version not in SUPPORTED_VERSIONS:
            raise ProjectError(f"project schema {version} is not supported")
        if version == 1:
            data = migrate_v1(data)
        info = data.get("project") or {}
        if not isinstance(info, dict) or not isinstance(info.get("target") or {}, dict):
            raise ProjectError("project: metadata must be an object")
        maps = data.get("maps") or {}
        if not isinstance(maps, dict):
            raise ProjectError("maps must be an object keyed by map key")
        for family in ("dependencies", *DEFINITION_FAMILIES, "extra"):
            if not isinstance(data.get(family) or {}, dict):
                raise ProjectError(f"{family} must be an object")
        if not isinstance(data.get("campaigns") or [], list):
            raise ProjectError("campaigns must be a list")
        project = cls(info.get("title", "Untitled"), (info.get("target") or {}).get("game", "studio"), info.get("id"))
        project.project = copy.deepcopy(info)
        project.dependencies = copy.deepcopy(data.get("dependencies") or {"sources": [], "starter": None})
        for key, map_data in sorted(maps.items()):
            check_key(key, "map key")
            if not isinstance(map_data, dict):
                raise ProjectError(f"map {key!r} must be an object")
            if not isinstance(map_data.get("scene"), str):
                raise ProjectError(f"map {key!r}: field scene must be a scene ID")
            for name, kind in (("base", dict), ("overlay", dict), ("view", dict), ("minimap", dict), ("extra", dict)):
                if map_data.get(name) is not None and not isinstance(map_data[name], kind):
                    raise ProjectError(f"map {key!r}: field {name} must be an object")
            _check_history(map_data.get("history"), f"map {key!r}")
            if map_data.get("kind") not in ("imported", "blank"):
                raise ProjectError(f"map {key!r} has unknown kind {map_data.get('kind')!r}")
            entry = MapEntry(key, map_data["kind"], map_data.get("title", key), map_data["scene"], map_data.get("source_fingerprint"),
                             copy.deepcopy(map_data.get("base")), copy.deepcopy(map_data.get("overlay")), copy.deepcopy(map_data.get("history")),
                             copy.deepcopy(map_data.get("view") or {"hidden": [], "locked": []}),
                             copy.deepcopy(map_data.get("minimap") or {"mode": "source"}), copy.deepcopy(map_data.get("extra") or {}))
            for unknown_key in sorted(set(map_data) - KNOWN_MAP):
                entry.extra.setdefault("_unknown", {})[unknown_key] = copy.deepcopy(map_data[unknown_key])
            if entry.kind == "blank" and not entry.base:
                raise ProjectError(f"blank map {key!r} has no base")
            project.maps[key] = entry
        project.campaigns = copy.deepcopy(data.get("campaigns") or [])
        for family in DEFINITION_FAMILIES:
            setattr(project, family, copy.deepcopy(data.get(family) or {}))
        project.extra = copy.deepcopy(data.get("extra") or {})
        project.definitions.load(data.get("definition_history"))
        project.unknown = {key: copy.deepcopy(value) for key, value in data.items() if key not in KNOWN_TOP}
        return project


def _record_ids(entry: MapEntry) -> set[str] | None:
    if entry.document is not None:
        return set(entry.document.scene.view)
    if entry.kind == "blank":
        ids = set()
        for edit in (entry.overlay or {}).get("edits", []):
            for key in ("marker", "instance", "collision"):
                if isinstance(edit.get(key), dict) and "id" in edit[key]:
                    ids.add(edit[key]["id"])
        return ids
    return None  # imported records are only known once the map is opened


def _source_key(source: dict) -> tuple:
    return (source.get("platform"), source.get("rom_sha256") or source.get("file_sha256") or source.get("rom_sha1") or "")


def _rank_sequences(data: dict) -> None:
    """Replace live sequence numbers with their rank (1..N) across every saved history.

    The live numbers come from a process-wide counter, so writing them would make the same project serialise
    differently from run to run.  Ranks keep the cross-history order (which Undo reverses first) and are deterministic.
    """

    entries = []
    for entry in (data.get("maps") or {}).values():
        history = entry.get("history") or {}
        entries += [item for name in ("undo", "redo") for item in history.get(name, [])]
    definition = data.get("definition_history") or {}
    entries += [item for name in ("undo", "redo") for item in definition.get(name, [])]
    numbered = sorted({item["sequence"] for item in entries if isinstance(item.get("sequence"), int) and item["sequence"] > 0})
    rank = {value: position + 1 for position, value in enumerate(numbered)}
    for item in entries:
        if item.get("sequence") in rank:
            item["sequence"] = rank[item["sequence"]]


def _content(data: dict) -> str:
    """Canonical project content without undo histories (used for the unsaved-changes state)."""

    data = dict(data)
    data.pop("definition_history", None)
    data["maps"] = {key: {k: v for k, v in entry.items() if k != "history"} for key, entry in (data.get("maps") or {}).items()}
    return json.dumps(data, sort_keys=True, ensure_ascii=False)


def migrate_v1(data: dict) -> dict:
    """v1 (one scene + overlay) -> v2 with a single imported map ``main``."""

    if "scene" not in data:
        raise ProjectError("version 1 project has no scene")
    return {
        "schema": PROJECT_SCHEMA, "schema_version": 2,
        "project": {"id": hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()[:16],
                    "title": data.get("title") or data["scene"], "description": "Migrated from a version 1 project",
                    "target": {"game": data.get("platform") if data.get("platform") in TARGET_GAMES else "studio", "package": "pc-port"}},
        "dependencies": {"sources": [], "starter": None},
        "maps": {"main": {"kind": "imported", "title": data.get("title") or data["scene"], "scene": data["scene"],
                          "source_fingerprint": data.get("source_fingerprint"), "overlay": data.get("overlay"),
                          "history": {"undo": [], "redo": []}, "view": {"hidden": [], "locked": []}, "minimap": {"mode": "source"}}},
        "campaigns": [], "missions": {}, "multiplayer": {}, "minigames": {}, "tanks": {}, "gangs": {},
    }


def dumps(project: StudioProject) -> str:
    text = json.dumps(project.as_dict(), sort_keys=True, indent=1, ensure_ascii=False, allow_nan=False) + "\n"
    overlay_module._refuse_host_paths(text)
    return text


def loads_project(text: str) -> StudioProject:
    try:
        data = json.loads(text)
    except ValueError as error:
        raise ProjectError(f"not a project file: {error}") from error
    return StudioProject.from_dict(data)


def save_project(path: Path, project: StudioProject) -> None:
    path = Path(path)
    text = dumps(project)  # serialise first: a failure here leaves the existing file untouched
    atomic_write_text(path, text)
    for entry in project.maps.values():
        if entry.document is not None:
            entry.document.mark_saved()
    project.path = path
    project.saved_text = text


def load_project(path: Path) -> StudioProject:
    project = loads_project(Path(path).read_text(encoding="utf-8"))
    project.path = Path(path)
    project.saved_text = dumps(project)
    return project


# ------------------------------------------------------ single-document helpers --

def project_for_document(document: EditorDocument, title: str | None = None) -> StudioProject:
    base = document.scene.base
    platform = base["source"].get("platform", "studio")
    project = StudioProject(title or base.get("title") or base["id"], platform if platform in TARGET_GAMES else "studio",
                            hashlib.sha256(base["id"].encode()).hexdigest()[:16])
    blank = bool(base["source"].get("blank"))
    entry = MapEntry("main", "blank" if blank else "imported", title or base.get("title") or base["id"], document.scene.level_id,
                     None if blank else scene_fingerprint(base), copy.deepcopy(base) if blank else None)
    entry.document = document
    project.maps["main"] = entry
    return project


def new_project(library, level_id: str) -> EditorDocument:
    return EditorDocument(EditorScene(library, level_id))


def dumps_document(document: EditorDocument, title: str | None = None) -> str:
    return dumps(project_for_document(document, title))


def save(path: Path, document: EditorDocument, title: str | None = None) -> None:
    save_project(path, project_for_document(document, title))


def loads(text: str, library) -> EditorDocument:
    project = loads_project(text)
    if len(project.maps) != 1:
        raise ProjectError("this helper opens single-map projects; use StudioProject for multi-map projects")
    return project.open_map(next(iter(project.maps)), library)


def load(path: Path, library) -> EditorDocument:
    return loads(Path(path).read_text(encoding="utf-8"), library)


# --------------------------------------------------------------- recovery --

class Autosaver:
    """Writes a recovery copy of a dirty project (or single document); off unless enabled."""

    def __init__(self, directory: Path, enabled: bool = False, interval: float = 120.0, clock=time.monotonic):
        self.directory = Path(directory)
        self.enabled = enabled
        self.interval = interval
        self.clock = clock
        self._last = clock()

    def recovery_path(self, item) -> Path:
        identity = item.project["id"] if isinstance(item, StudioProject) else item.scene.level_id
        return self.directory / f"{hashlib.sha256(identity.encode()).hexdigest()[:16]}.recovery.btgaproj"

    def tick(self, item) -> Path | None:
        if not self.enabled or item is None or not item.dirty:
            return None
        now = self.clock()
        if now - self._last < self.interval:
            return None
        self._last = now
        return self.write(item)

    def write(self, item) -> Path:
        path = self.recovery_path(item)
        path.parent.mkdir(parents=True, exist_ok=True)
        text = dumps(item) if isinstance(item, StudioProject) else dumps_document(item)
        atomic_write_text(path, text)
        meta = {"title": item.project.get("title") if isinstance(item, StudioProject) else item.scene.level_id,
                "project_path": str(item.path) if isinstance(item, StudioProject) and item.path else None}
        atomic_write_text(path.with_name(path.name + ".json"), json.dumps(meta, sort_keys=True))
        return path

    def discard(self, item) -> None:
        path = self.recovery_path(item)
        for target in (path, path.with_name(path.name + ".json")):
            try:
                target.unlink()
            except OSError:
                pass

    def recoveries(self) -> list[dict]:
        """Recovery files left behind (for example after a crash), newest first."""

        out = []
        for path in sorted(self.directory.glob("*.recovery.btgaproj")):
            try:
                project = loads_project(path.read_text(encoding="utf-8"))
            except (OSError, ProjectError):
                continue
            try:
                meta = json.loads(path.with_name(path.name + ".json").read_text(encoding="utf-8"))
            except (OSError, ValueError):
                meta = {}
            out.append({"path": path, "title": project.project.get("title"), "project_path": meta.get("project_path"),
                        "modified": path.stat().st_mtime})
        return sorted(out, key=lambda r: (-r["modified"], str(r["path"])))


def export_preview(directory: Path, document: EditorDocument, library, diagnostics: list | None = None) -> list[Path]:
    """Inspection package: project, edited neutral scene, validation report and a plan image.  No ROM patching."""

    from tools.btga_assets import preview

    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    edited = document.scene.edited_scene(document.overlay)
    written = []
    files = {
        "project.btgaproj": dumps_document(document),
        "edited_scene.json": json.dumps(edited, sort_keys=True, indent=1) + "\n",
        "validation.json": json.dumps([d.as_dict() if hasattr(d, "as_dict") else d for d in (diagnostics or [])], sort_keys=True, indent=1) + "\n",
        "README.txt": "BattleTanx Studio preview package: inspection only. It is not a mod pack and patches nothing.\n",
    }
    for name, text in files.items():
        (directory / name).write_text(text, encoding="utf-8")
        written.append(directory / name)
    (directory / "plan.png").write_bytes(preview.plan_view(edited).png())
    written.append(directory / "plan.png")
    return written
