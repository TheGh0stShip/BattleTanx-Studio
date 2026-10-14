"""Editor-facing resource API for BTGA Studio (platform-neutral).

Studio reads one exported catalog (export_neutral_assets.py) and never
branches on the source platform: every map, model and texture arrives in
the same neutral shape.  `platform` stays available as provenance only.

    library = StudioLibrary.open(export_dir)
    for level in library.levels():
        game_map = library.load_map(level.id)
        for instance in game_map.instances:
            mesh = library.model(instance.model).mesh

Loading is lazy: ``open`` reads only catalog.json.  Maps, models, textures
and thumbnails are read on request; thumbnails sit in a bounded cache, and
nothing else is cached, so repeated map cycles keep memory flat.

Editing never touches source records: ``new_overlay`` starts a project
overlay, ``load_map(level, overlay)`` shows the edited map and
``validate_overlay`` reports structured errors (see overlay.py).
"""

from __future__ import annotations

import json
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path

from . import overlay as overlay_module
from .compare import png_rgba
from .export import CATALOG_SCHEMA
from .neutral import safe_source_path

CATALOG_VERSIONS = (1, 2)
THUMBNAIL_CACHE = 256


class StudioError(ValueError):
    pass


@dataclass(frozen=True)
class Source:
    platform: str
    title: str
    identity: dict


@dataclass(frozen=True)
class LevelSummary:
    id: str
    name: str
    campaign: str | None
    variant: str
    platform: str
    instances: int
    markers: int
    collision: int
    variants: tuple[str, ...] = ()


@dataclass(frozen=True)
class Instance:
    id: str
    model: str
    position: tuple[float, float, float]
    yaw_turns: float
    category: str


@dataclass(frozen=True)
class CollisionShape:
    """A collision volume.  ``local_min``/``local_max`` y is None when the game stores no height."""

    id: str
    kind: str
    position: tuple[float, float, float]
    yaw_turns: float
    local_min: tuple
    local_max: tuple
    flags: object = None
    vertical: dict | None = None
    orientation: str | None = None
    confidence: str | None = None
    disabled: bool = False
    edited: bool = False


@dataclass(frozen=True)
class Marker:
    id: str
    category: str
    position: tuple[float, float, float]
    yaw_turns: float
    models: tuple[str, ...] = ()
    source_kind: str | None = None
    fields: dict = field(default_factory=dict)
    dependencies: tuple = ()
    restrictions: dict = field(default_factory=dict)
    confidence: str | None = None
    disabled: bool = False
    edited: bool = False
    source: dict = field(default_factory=dict)


@dataclass
class GameMap:
    id: str
    name: str
    instances: list[Instance]
    collision: list[CollisionShape]
    markers: list[Marker]
    bounds: dict
    environment: list[dict]
    minimap: dict
    navigation: dict | None
    provenance: dict = field(default_factory=dict)
    floor: dict | None = None
    platform: str | None = None
    overlay: dict | None = None

    def placeable_models(self) -> list[str]:
        return sorted({instance.model for instance in self.instances} | {m for marker in self.markers for m in marker.models})


@dataclass
class Model:
    id: str
    mesh: dict
    bounds: dict | None
    provenance: dict


@dataclass
class Texture:
    id: str
    width: int
    height: int
    wrap: dict
    alpha: str | None
    rgba: bytes


def _turns(units) -> float:
    return (int(units) & 0xFFFF) / 65536.0


def _tuple(values) -> tuple:
    return tuple(None if v is None else float(v) for v in values)


class StudioLibrary:
    def __init__(self, root: Path, catalog: dict):
        if catalog.get("schema") != CATALOG_SCHEMA or catalog.get("schema_version") not in CATALOG_VERSIONS:
            raise StudioError("not a btga-catalog export")
        self.root = Path(root)
        self.catalog = catalog
        self._levels = {level["scene"]: level for level in catalog["levels"]}
        self._models = {model["id"]: model for model in catalog["models"]}
        self._textures = {texture["id"]: texture for texture in catalog["textures"]}
        self._thumbnails: OrderedDict[tuple, bytes] = OrderedDict()

    @classmethod
    def open(cls, root: Path) -> "StudioLibrary":
        return cls(root, json.loads((Path(root) / "catalog.json").read_text()))

    def _read(self, relative: str) -> bytes:
        return (self.root / safe_source_path(relative)).read_bytes()

    # -- enumeration (catalog only; no map, model or texture is read) ----------
    def sources(self) -> list[Source]:
        return [Source(s["platform"], s["title"], {k: v for k, v in s.items() if k not in ("platform", "title")}) for s in self.catalog["sources"]]

    def campaigns(self) -> dict[str, list[str]]:
        out: dict[str, list[str]] = {}
        for level in self.catalog["levels"]:
            out.setdefault(level.get("campaign") or "unassigned", []).append(level["scene"])
        return {key: sorted(value) for key, value in sorted(out.items())}

    def levels(self, campaign: str | None = None) -> list[LevelSummary]:
        result = []
        for level in self.catalog["levels"]:
            if campaign is not None and level.get("campaign") != campaign:
                continue
            result.append(LevelSummary(level["scene"], level.get("name") or str(level["level"]), level.get("campaign"), level.get("variant", "default"),
                                       level["platform"], level["instances"], level["markers"], level["collision"],
                                       tuple(level.get("variants", [level.get("variant", "default")]))))
        return result

    def models(self) -> list[str]:
        return sorted(self._models)

    def textures(self) -> list[str]:
        return sorted(texture_id for texture_id, texture in self._textures.items() if texture.get("file"))

    def library_models(self) -> dict[str, list[str]]:
        """Models not tied to one map (tank/weapon/effect banks), grouped by library."""

        out: dict[str, list[str]] = {}
        for model_id, entry in self._models.items():
            if "library" in entry:
                library = entry["library"]
            else:  # catalog v1: the library tag lives only in the model record
                library = json.loads(self._read(entry["file"]))["source"].get("library")
            if library:
                out.setdefault(library, []).append(model_id)
        return {key: sorted(value) for key, value in sorted(out.items())}

    # -- loading -------------------------------------------------------------
    def scene(self, level_id: str) -> dict:
        if level_id not in self._levels:
            raise StudioError(f"unknown level {level_id!r}")
        return json.loads(self._read(self._levels[level_id]["file"]))

    def load_map(self, level_id: str, overlay: overlay_module.Overlay | None = None) -> GameMap:
        scene = self.scene(level_id)
        if overlay is not None:
            scene = overlay_module.apply(scene, overlay)
        return self._game_map(level_id, scene)

    def _game_map(self, level_id: str, scene: dict) -> GameMap:
        instances = [Instance(i["id"], i["model"], _tuple(i["translation"]), _turns(i["yaw"]), i.get("category", "model"))
                     for i in scene["instances"]]
        collision = [CollisionShape(c["id"], c.get("shape", "box"), _tuple(c["translation"]), _turns(c["yaw"]), _tuple(c["min"]), _tuple(c["max"]),
                                    c.get("flags", c.get("extras", {}).get("flags")), c.get("vertical"), c.get("orientation"), c.get("confidence"),
                                    bool(c.get("disabled")), bool(c.get("overlay")))
                     for c in scene["collision"]]
        markers = [Marker(m["id"], m["category"], _tuple(m["translation"]), _turns(m.get("yaw", 0)), tuple(m.get("models", ())),
                          m.get("source_kind"), m.get("fields", {}), tuple(m.get("dependencies", ())), m.get("restrictions", {}),
                          m.get("confidence"), bool(m.get("disabled")), bool(m.get("overlay")), m.get("source", {}))
                   for m in scene["markers"]]
        return GameMap(scene["id"], self._levels[level_id].get("name") or scene["id"], instances, collision, markers, scene.get("bounds", {}),
                       scene.get("environment", []), scene.get("minimap", {}), scene.get("navigation"), scene["source"],
                       scene.get("floor"), scene["source"].get("platform"), scene.get("overlay"))

    def model(self, model_id: str) -> Model:
        if model_id not in self._models:
            raise StudioError(f"unknown model {model_id!r}")
        record = json.loads(self._read(self._models[model_id]["file"]))
        return Model(model_id, record["mesh"], record.get("bounds"), record["source"])

    def texture(self, texture_id: str) -> Texture:
        entry = self._textures.get(texture_id)
        if entry is None or not entry.get("file"):
            raise StudioError(f"texture {texture_id!r} has no decoded image")
        width, height, rgba = png_rgba(self._read(entry["file"]))
        return Texture(texture_id, width, height, entry.get("wrap") or {"s": "clamp", "t": "clamp"}, entry.get("alpha"), rgba)

    def thumbnail(self, model_id: str, size: int = 96) -> bytes:
        """RGBA thumbnail rendered on first request and kept in a bounded LRU cache."""

        key = (model_id, size)
        if key in self._thumbnails:
            self._thumbnails.move_to_end(key)
            return self._thumbnails[key]
        from .preview import model_thumbnail

        model = self.model(model_id)
        textures = {}
        for material in model.mesh["materials"]:
            texture_id = material.get("texture")
            if texture_id and texture_id not in textures and texture_id in self._textures and self._textures[texture_id].get("file"):
                texture = self.texture(texture_id)
                textures[texture_id] = {"width": texture.width, "height": texture.height, "rgba": texture.rgba, "wrap": texture.wrap}
        pixels = model_thumbnail(model.mesh, textures, size)
        self._thumbnails[key] = pixels
        while len(self._thumbnails) > THUMBNAIL_CACHE:
            self._thumbnails.popitem(last=False)
        return pixels

    # -- minimaps --------------------------------------------------------------
    def minimap(self, level_id: str, variant: str | None = None) -> dict:
        """The selected minimap for one loading variant: status, texture, transform."""

        record = self.scene(level_id).get("minimap", {})
        selections = record.get("selections", [])
        chosen = next((s for s in selections if variant is None or s["variant"] == variant), None)
        out = {"status": record.get("status"), "selection": chosen, "transform": record.get("transform"),
               "origin": record.get("origin"), "scale": record.get("scale"), "texture": None}
        if chosen and chosen.get("image") and chosen["image"].get("texture") in self._textures:
            out["texture"] = self.texture(chosen["image"]["texture"])
        return out

    @staticmethod
    def world_to_minimap(minimap: dict, x: float, z: float) -> tuple[int, int] | None:
        """func_800C7C10's mapping; None when the image would not draw the point."""

        if not minimap.get("scale") or not minimap.get("selection") or not minimap["selection"].get("image"):
            return None
        px = int((x - minimap["origin"][0]) * minimap["scale"])
        py = int((z - minimap["origin"][1]) * minimap["scale"])
        image = minimap["selection"]["image"]
        if 0 <= px < image["width"] and 0 <= py < image["height"]:
            return px, py
        return None

    # -- editing ---------------------------------------------------------------
    def new_overlay(self, level_id: str) -> overlay_module.Overlay:
        return overlay_module.Overlay.for_scene(self.scene(level_id))

    def validate_overlay(self, level_id: str, overlay: overlay_module.Overlay, target_platform: str | None = None) -> list[dict]:
        return overlay_module.validate(self.scene(level_id), overlay, set(self._models), set(self.textures()), target_platform)

    @staticmethod
    def save_overlay(overlay: overlay_module.Overlay, path: Path) -> None:
        Path(path).write_text(overlay.to_json())

    @staticmethod
    def load_overlay(path: Path) -> overlay_module.Overlay:
        return overlay_module.Overlay.from_json(Path(path).read_text())

    def marker_templates(self, level_id: str) -> dict[str, dict]:
        """One source marker per source kind in this map, usable as an add_marker template."""

        out = {}
        for marker in self.scene(level_id)["markers"]:
            out.setdefault(marker.get("source_kind"), marker)
        return out

    # -- editor views --------------------------------------------------------
    def placeable_objects(self) -> list[dict]:
        categories: dict[str, set] = {}
        for level_id in self._levels:
            game_map = self.load_map(level_id)
            for instance in game_map.instances:
                categories.setdefault("model", set()).add(instance.model)
            for marker in game_map.markers:
                categories.setdefault(marker.category, set()).update(marker.models)
        return [{"category": key, "models": sorted(value)} for key, value in sorted(categories.items())]

    def environment_presets(self) -> dict[str, list[dict]]:
        return {level_id: self.load_map(level_id).environment for level_id in sorted(self._levels)}

    def minimaps(self) -> dict[str, dict]:
        return {level_id: self.load_map(level_id).minimap for level_id in sorted(self._levels)}

    def gangs_and_tanks(self) -> dict:
        """Shared vehicle/object banks (N64 common world, PS1 ENUMS).

        No traced table maps individual gang or tank names onto these models,
        so they are offered as libraries rather than named entries.
        """

        return {"libraries": self.library_models(), "names_mapped": False}
