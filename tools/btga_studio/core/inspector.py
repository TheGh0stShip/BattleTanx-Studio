"""Inspector controls generated from typed field schemas.

``describe`` turns one record into an ``Inspector``: a specialised title and
kind (spawn, generator, pickup, objective, hazard, collision, navigation,
minimap, environment, prop) and a list of ``Control`` objects carrying the
control kind, value, range, unit, evidence and whether it may be edited.
``parse_value`` converts text from a widget and validates it before any
edit transaction exists.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from tools.btga_assets.semantics import field_errors

from .document import EditError, field_editable, is_read_only

TEAM_FIELDS = {"team", "team_slot", "owner_slot", "team_index", "slot"}
SPECIALISED = {
    "unit_spawner": "Unit generator", "tank_generator": "Unit generator",
    "team_spawn_slot": "Spawn / team start", "team_start": "Spawn / team start", "team_point_a": "Spawn / team start",
    "team_point_b": "Spawn / team start", "multiplayer_structure": "Spawn / team start",
    "pickup": "Pickup", "powerup_pad": "Pickup", "crate": "Pickup",
    "objective": "Objective", "mission_trigger": "Objective",
    "hazard": "Hazard",
}
UNITS = {"spawn_interval": "ticks", "hazard_size": "", "half_width_x": "world units", "half_width_z": "world units"}


@dataclass
class Control:
    name: str
    label: str
    kind: str  # bool int float angle enum bitmask reference slot position bounds colour text
    value: object
    editable: bool
    reason: str | None = None
    range: tuple | None = None
    unit: str = ""
    options: dict = field(default_factory=dict)
    bits: int = 0
    evidence: str = ""
    meaning: str = ""
    spec: dict | None = None


@dataclass
class Inspector:
    title: str
    kind: str
    record_id: str | None
    controls: list[Control]
    notes: list[str] = field(default_factory=list)
    read_only_reason: str | None = None


def control_for(name: str, spec: dict, record_reason: str | None) -> Control:
    reason = record_reason or field_editable(spec)
    lo_hi = tuple(spec["range"]) if "range" in spec else None
    if spec["type"] == "ref":
        return Control(name, name.replace("_", " ").capitalize(), "record", spec["value"], reason is None, reason, None, "", {}, 0,
                       spec.get("evidence", ""), spec.get("meaning", ""), spec)
    if spec["type"] == "text":
        return Control(name, name.replace("_", " ").capitalize(), "text", spec["value"], reason is None, reason, (0, spec.get("max_length", 256)),
                       "characters", {}, 0, spec.get("evidence", ""), spec.get("meaning", ""), spec)
    if spec.get("reference") == "model":
        kind = "reference"
    elif name in TEAM_FIELDS:
        kind = "slot"
    elif "bitmask_bits" in spec:
        kind = "bitmask"
    elif "enum" in spec:
        kind = "enum"
    elif lo_hi == (0, 1):
        kind = "bool"
    elif spec["type"] == "f32":
        kind = "float"
    else:
        kind = "int"
    return Control(name, name.replace("_", " ").capitalize(), kind, spec["value"], reason is None, reason, lo_hi, UNITS.get(name, ""),
                   dict(spec.get("enum", {})) | {f"{k} (special)": v for k, v in spec.get("sentinel", {}).items()} if kind == "enum" else
                   {k: v for k, v in spec.get("sentinel", {}).items()}, spec.get("bitmask_bits", 0), spec.get("evidence", ""),
                   spec.get("meaning", ""), spec)


def describe(record: dict, family: str) -> Inspector:
    reason = is_read_only(record) if family == "markers" else None
    transform = [Control("translation", "Position", "position", list(record["translation"]), reason is None, reason, unit="world units"),
                 Control("yaw", "Yaw", "angle", record["yaw"], reason is None, reason, (0, 0xFFFF), "units (65536 per turn)")]
    if family == "instances":
        controls = transform + [Control("model", "Model", "reference", record["model"], False,
                                        "the model of a placed object is fixed; place a new object to change it")]
        return Inspector("Visual prop", "prop", record["id"], controls)
    if family == "collision":
        vertical = record.get("vertical", {})
        planar = vertical.get("status") != "proven"
        controls = transform + [Control("bounds", "Extent (local min / max)", "bounds", [list(record["min"]), list(record["max"])], True,
                                        unit="world units")]
        notes = [f"Vertical: {vertical.get('status')} - {vertical.get('evidence', '')}", f"Orientation: {record.get('orientation')}"]
        if planar:
            notes.append("This platform's collision is planar: heights cannot be set.")
        return Inspector("Collision shape", "collision", record["id"], controls, notes)
    controls = transform + [control_for(name, spec, reason) for name, spec in sorted(record.get("fields", {}).items())]
    title = SPECIALISED.get(record.get("category"), "Gameplay marker")
    notes = [f"Source kind {record.get('source_kind')}; confidence {record.get('confidence')}"] + list(record.get("evidence", []))
    for key, value in sorted(record.get("restrictions", {}).items()):
        notes.append(f"Restriction {key}: {value}")
    if record.get("unknown_bytes"):
        notes.append(f"{len(record['unknown_bytes'])} raw byte(s) are preserved unchanged: offsets {record['unknown_bytes'][:12]}")
    return Inspector(title, title.split(" ")[0].lower(), record["id"], controls, notes, reason)


def describe_environment(entry: dict) -> Inspector:
    controls = []
    for key, value in sorted(entry.items()):
        if isinstance(value, list) and len(value) == 3 and all(isinstance(v, int) for v in value):
            controls.append(Control(key, key.replace("_", " ").capitalize(), "colour", value, False,
                                    "environment records have no overlay edit yet"))
    return Inspector("Environment / fog / background", "environment", None, controls, [entry.get("evidence", "")])


def describe_minimap(minimap: dict) -> Inspector:
    controls = [Control("origin", "Origin", "position", minimap.get("origin"), False, "derived from the map box by the game"),
                Control("scale", "Scale", "float", minimap.get("scale"), False, "derived from the map box by the game")]
    variants = {s["variant"]: s.get("status") for s in minimap.get("selections", [])}
    controls.append(Control("variant", "Source image for variant", "enum", next(iter(variants), None), bool(variants), None, options=variants))
    return Inspector("Minimap alignment", "minimap", None, controls, [minimap.get("transform", "")])


def describe_nav_point(navigation: dict, index: int) -> Inspector:
    point = navigation["points"][index]
    links = [link for link in navigation.get("links", []) if index in link["targets"]]
    controls = [Control("position", "Position", "position", [point["x"], 0.0, point["z"]], False, "navigation point positions have no overlay edit"),
                Control("links", "Links targeting this point", "text", [link["index"] for link in links], False, "edit links individually")]
    return Inspector(f"Navigation point {index}", "navigation", f"nav:{index}", controls, [navigation.get("evidence", "")])


def parse_value(control: Control, text) -> object:
    """Convert widget input to a typed value and validate it; raises EditError."""

    if not control.editable:
        raise EditError(f"{control.label} cannot be edited: {control.reason}")
    try:
        if control.kind == "record":
            value = text if text else None
        elif control.kind == "text":
            value = str(text)
        elif control.kind in ("int", "slot", "enum", "bitmask", "bool", "reference", "angle"):
            value = int(text, 0) if isinstance(text, str) else int(text)
        elif control.kind == "float":
            value = float(text)
        else:
            value = text
    except (TypeError, ValueError) as error:
        raise EditError(f"{control.label}: {error}") from error
    if control.spec is not None:
        problems = field_errors(control.name, control.spec, value)
        if problems:
            raise EditError("; ".join(problems))
    elif control.range and control.kind not in ("text", "record") and not control.range[0] <= value <= control.range[1]:
        raise EditError(f"{control.label}: {value} outside {list(control.range)}")
    return value
