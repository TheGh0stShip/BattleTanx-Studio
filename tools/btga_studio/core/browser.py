"""Asset browser index built from catalog metadata only.

Model and texture entries come from catalog.json (no geometry is decoded).
Gameplay templates, environment records and user-created records come from
the currently open map, which is already loaded.  Filtering never re-reads
any file; it runs over the prebuilt entry list.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .document import is_read_only

GAMES = {"n64_btga": "Global Assault (N64)", "ps1": "Global Assault (PS1)", "n64_bt1": "BattleTanx (N64)", "studio": "Studio starter"}
GAMEPLAY_GROUPS = {
    "Spawns": {"team_spawn_slot", "team_start", "team_point_a", "team_point_b", "multiplayer_structure", "point_list_entry", "player_spawn"},
    "Navigation": {"navigation_node"},
    "Unit generators": {"unit_spawner", "tank_generator"},
    "Pickups": {"pickup", "powerup_pad", "crate"},
    "Objectives": {"objective", "mission_trigger"},
    "Hazards": {"hazard"},
}


@dataclass(frozen=True)
class AssetEntry:
    id: str
    kind: str  # model texture gameplay environment
    category: str
    platform: str | None
    label: str
    library: str | None = None
    source: str = "source"  # source | user
    editable: bool = True
    detail: str = ""
    model: str | None = None  # model ID drawn for this entry (differs from ``id`` for user-placed instances)
    search: str = field(default="", compare=False)

    @property
    def model_id(self) -> str | None:
        return self.model or (self.id if self.kind == "model" and self.source != "user" else None)


def _entry(**kwargs) -> AssetEntry:
    text = " ".join(str(v) for v in kwargs.values() if v).lower()
    return AssetEntry(**kwargs, search=text)


class AssetIndex:
    def __init__(self, library):
        self.library = library
        self.catalog_entries: list[AssetEntry] = []
        for model in library.catalog["models"]:
            category = "Vehicles & object library" if model.get("library") else "Models"
            if (model.get("library") or "").startswith("starter:"):
                category = "Starter " + model["library"].split(":", 1)[1].lower()
            self.catalog_entries.append(_entry(id=model["id"], kind="model", category=category, platform=model.get("platform"),
                                               label=model.get("label") or model["id"].rsplit(":", 1)[-1][:12], library=model.get("library"),
                                               detail=f"{model.get('triangles', 0)} triangles"))
        for texture in library.catalog["textures"]:
            if not texture.get("file"):
                continue
            self.catalog_entries.append(_entry(id=texture["id"], kind="texture", category="Textures",
                                               platform=(texture.get("source") or {}).get("platform"),
                                               label=texture["id"].rsplit(":", 1)[-1][:12], detail=f"{texture['width']}x{texture['height']} {texture.get('format', '')}"))
        self.map_entries: list[AssetEntry] = []
        self.map_models: set[str] = set()
        self.templates: dict[str, dict] = {}
        self.template_entries: list[AssetEntry] = []
        self.reads = 0

    def add_templates(self, templates: list[dict]) -> None:
        """Starter gameplay templates: placeable in every map, whatever its source."""

        self.templates = {t["id"]: t for t in templates}
        self.template_entries = []
        for template in templates:
            group = next((name for name, members in GAMEPLAY_GROUPS.items() if template.get("category") in members), "Other gameplay")
            self.template_entries.append(_entry(id=template["id"], kind="gameplay", category=group, platform="studio",
                                                label=template["category"].replace("_", " "), source="starter",
                                                detail="Studio starter template"))

    def set_map(self, scene, user_ids: set[str] | None = None) -> None:
        """Index the open map's gameplay templates, environment records and user records."""

        self.map_entries = []
        self.map_models = set(scene.base.get("models", []))
        platform = scene.base["source"].get("platform")
        seen_kinds = set()
        for family, record in scene.records("markers"):
            user = record.get("source", {}).get("user_created", False)
            if not user and record.get("source_kind") in seen_kinds:
                continue
            seen_kinds.add(record.get("source_kind"))
            group = next((name for name, members in GAMEPLAY_GROUPS.items() if record.get("category") in members), "Other gameplay")
            reason = is_read_only(record)
            self.map_entries.append(_entry(id=record["id"], kind="gameplay", category=group, platform=platform,
                                           label=f"{str(record.get('category')).replace('_', ' ')}{' (placed)' if user else ''}", source="user" if user else "source",
                                           editable=reason is None, detail=reason or record.get("confidence", "")))
        for number, env in enumerate(scene.base.get("environment", [])):
            self.map_entries.append(_entry(id=f"environment:{number}", kind="environment", category="Environment", platform=platform,
                                           label="Map sky and fog", editable=False,
                                           detail="shown in the inspector when nothing is selected"))
        for family, record in scene.records("instances"):
            if record.get("source", {}).get("user_created"):
                self.map_entries.append(_entry(id=record["id"], kind="model", category="User-placed", platform=platform,
                                               label=record["model"].rsplit(":", 1)[-1][:12], source="user", model=record["model"]))

    def filter(self, text: str = "", platform: str | None = None, kind: str | None = None, category: str | None = None,
               map_only: bool = False, library: str | None = None) -> list[AssetEntry]:
        needle = text.lower().strip()
        out = []
        for entry in self.template_entries + self.map_entries + self.catalog_entries:
            if platform and entry.platform != platform:
                continue
            if kind and entry.kind != kind:
                continue
            if category and entry.category != category:
                continue
            if library and entry.library != library:
                continue
            if map_only and entry.kind == "model" and entry.source == "source" and entry.id not in self.map_models \
                    and entry.platform != "studio":
                continue
            if map_only and entry.kind == "texture":
                continue
            if needle and needle not in entry.search:
                continue
            out.append(entry)
        return out

    def categories(self) -> list[str]:
        return sorted({entry.category for entry in self.template_entries + self.map_entries + self.catalog_entries})
