"""The Studio side of "Test in Game": runtime capabilities, feature requirements and the launch request.

Studio never runs content itself and never claims content is playable.  A
PC runtime declares what it can load and play in a sidecar file next to its
executable, ``btga-runtime.json``::

    {"schema": "btga-runtime-capabilities", "schema_version": 1,
     "runtime": {"name": "...", "version": "1.2.3"},
     "launch_request_versions": [1], "package_schema_versions": [1],
     "games": ["n64_btga"], "features": ["map.blank", "scenario", ...],
     "bot_slots": 8, "teams": 4, "capture_modes": ["none", "screenshot", "video"]}

Studio reads that file; it does not execute the program to ask.  A package's
content needs the features ``project_features`` lists (``FEATURES`` explains
each).  ``playability`` compares the two: "unverified" when no runtime is
configured, "supported" when the runtime declares every feature, otherwise
"unsupported" with the missing features.  Exported packages record this in
their manifest; nothing is ever marked playable without a runtime declaring it.

The launch request (``btga-launch-request`` v1) is written to the runtime
process's standard input as one JSON document and never to disk or into the
project, because it carries the local ROM path::

    {"schema": "btga-launch-request", "schema_version": 1, "session_id": "<32 hex>",
     "package": {"path": "<absolute>", "sha256": "<64 hex>", "size": n},
     "map": "<map key>", "scenario": "<scenario key>" | null, "player_spawn": "<record id>" | null,
     "bot_slots": [n, ...], "capture": {"mode": "none" | "screenshot" | "video", "directory": "<absolute>" | null},
     "rom": {"path": "<absolute>", "sha256": "<64 hex>"},
     "studio": {"version": "<version>"}, "features": ["..."]}

``preflight`` checks everything before a launch and reports, each with an
actionable message: no runtime configured or present (RUNTIME_ABSENT), an
incompatible runtime (RUNTIME_INCOMPATIBLE), a missing or stale package
(PACKAGE_MISSING, PACKAGE_STALE), a missing or wrong ROM (ROM_MISSING,
ROM_MISMATCH), content the runtime rejects (CONTENT_REJECTED) and an invalid
selection (INVALID_SELECTION).  Only a plan without problems can launch.
"""

from __future__ import annotations

import hashlib
import json
import re
import secrets
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from . import actions as act
from . import bots as bots_module
from . import items as items_module
from . import scenario as scenario_module

CAPABILITIES_SCHEMA = "btga-runtime-capabilities"
CAPABILITIES_VERSION = 1
SIDECAR = "btga-runtime.json"
LAUNCH_SCHEMA = "btga-launch-request"
LAUNCH_VERSION = 1
CAPTURE_MODES = ("none", "screenshot", "video")
LAUNCH_ARGUMENT = "--btga-launch-request"
MAX_SIDECAR = 1 << 20
HEX64 = re.compile(r"^[0-9a-f]{64}$")


def _features() -> dict[str, str]:
    out = {
        "map.blank": "maps authored in Studio (records, collision and environment travel in the package)",
        "map.imported": "original game maps with an overlay of edits, resolved from the runtime's own extracted data",
        "map.sky_panorama": "a sky panorama picture on a Studio map",
        "resource.studio": "Studio starter models and textures embedded in the package",
        "resource.user": "user-imported models and textures embedded in the package",
        "resource.game": "game models and textures referenced by content ID and resolved from the local cartridge",
        "marker.teleporter": "teleporter entrance and exit pairs",
        "marker.trigger_volume": "trigger volumes",
        "marker.capture_zone": "capture and defend zones",
        "marker.destructible_target": "destructible targets",
        "marker.item_pickup": "item pickups placed on the map",
        "marker.hazard": "damage areas", "marker.unit_spawner": "unit generators", "marker.navigation_node": "Studio navigation points",
        "marker.pickup": "starter pickups", "marker.objective": "objective markers", "marker.spawn": "player spawns and team starts",
        "scenario": "scenarios (rules, objectives, triggers)", "scenario.timers": "timers", "scenario.counters": "counters",
        "scenario.waves": "waves", "scenario.groups": "object groups", "scenario.score_multipliers": "score multipliers",
        "objective.flow.requires": "ordered and parallel objectives", "objective.flow.optional": "optional objectives",
        "objective.flow.repeat": "repeatable objectives", "item": "custom item definitions", "bot": "bot slots", "bot.respawn": "bot respawning",
    }
    for mode in scenario_module.MODES:
        out[f"scenario.mode.{mode}"] = f"match mode {mode.replace('_', ' ')}"
    for kind, spec in scenario_module.OBJECTIVE_KINDS.items():
        out[f"objective.{kind}"] = f"objective: {spec['label'].lower()}"
    for event in scenario_module.TRIGGER_EVENTS:
        out[f"trigger.{event}"] = f"trigger on {event.replace('_', ' ')}"
    for condition in scenario_module.CONDITIONS:
        out[f"condition.{condition}"] = f"victory/failure condition {condition.replace('_', ' ')}"
    for where, values in (("start", scenario_module.TRANSITIONS_START), ("victory", scenario_module.TRANSITIONS_VICTORY),
                          ("failure", scenario_module.TRANSITIONS_FAILURE)):
        for value in values:
            out[f"transition.{where}.{value}"] = f"{where} transition {value.replace('_', ' ')}"
    for kind, spec in act.ACTIONS.items():
        out[act.feature(kind)] = f"action: {spec['label'].lower()}"
    for category in items_module.CATEGORIES:
        out[f"item.category.{category}"] = f"item category {category}"
    for stacking in items_module.STACKING:
        out[f"item.stacking.{stacking}"] = f"item stacking {stacking}"
    for role in bots_module.ROLES:
        out[f"bot.role.{role}"] = f"bot behaviour {role}"
    for kind in bots_module.KINDS:
        out[f"bot.kind.{kind}"] = f"bot kind {kind}"
    return dict(sorted(out.items()))


FEATURES = _features()
MARKER_FEATURES = {"teleporter": "marker.teleporter", "teleport_exit": "marker.teleporter", "trigger_volume": "marker.trigger_volume",
                   "capture_zone": "marker.capture_zone", "destructible_target": "marker.destructible_target",
                   "item_pickup": "marker.item_pickup", "hazard": "marker.hazard", "unit_spawner": "marker.unit_spawner",
                   "navigation_node": "marker.navigation_node", "pickup": "marker.pickup", "objective": "marker.objective",
                   "player_spawn": "marker.spawn", "team_start": "marker.spawn"}


class RuntimeProblem(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


# ---------------------------------------------------------------- requirements --

def map_features(entry, document) -> dict[str, list[str]]:
    """feature -> records that need it, for one open map."""

    out: dict[str, set] = {}

    def need(name, where):
        out.setdefault(name, set()).add(where)

    need("map.blank" if entry.kind == "blank" else "map.imported", f"maps:{entry.key}")
    if entry.kind == "blank" and (entry.base or {}).get("environment", [{}])[0].get("sky"):
        need("map.sky_panorama", f"maps:{entry.key}")
    for family, record in document.scene.records():
        if record.get("disabled"):
            continue
        if family == "instances":
            platform = str(record.get("model", "")).split(":", 1)[0]
            need({"studio": "resource.studio", "user": "resource.user"}.get(platform, "resource.game"), record["id"])
        elif family == "markers" and record.get("overlay"):
            category = record.get("category")
            if category in MARKER_FEATURES:
                need(MARKER_FEATURES[category], record["id"])
            if category == "bot_slot":
                for name in bots_module.required_features(record):
                    need(name, record["id"])
                model = bots_module.value(record, "model")
                if model:
                    need({"studio": "resource.studio", "user": "resource.user"}.get(str(model).split(":", 1)[0], "resource.game"), record["id"])
    return {name: sorted(where) for name, where in sorted(out.items())}


def project_features(project, documents: dict) -> dict[str, list[str]]:
    """feature -> what needs it (scenarios, items, map records) for a whole project; ``documents`` maps map key -> open document."""

    out: dict[str, set] = {}
    for key, entry in sorted(project.maps.items()):
        document = documents.get(key)
        if document is None:
            continue
        for name, where in map_features(entry, document).items():
            out.setdefault(name, set()).update(where)
    for key, scenario in sorted(project.scenarios.items()):
        for name in scenario_module.required_features(scenario):
            out.setdefault(name, set()).add(f"scenarios:{key}")
    for key, item in sorted(project.items.items()):
        for name in items_module.required_features(item):
            out.setdefault(name, set()).add(f"items:{key}")
    return {name: sorted(where) for name, where in sorted(out.items())}


# ---------------------------------------------------------------- capabilities --

def read_capabilities(executable: Path) -> dict:
    """The runtime's declared capabilities; raises RuntimeProblem when it is absent or not compatible."""

    if executable is None or not str(executable):
        raise RuntimeProblem("RUNTIME_ABSENT", "no PC runtime is configured; choose its program in Settings > Test in Game")
    executable = Path(executable)
    if not executable.is_file():
        raise RuntimeProblem("RUNTIME_ABSENT", f"the configured PC runtime {executable.name} is not there any more; choose it again in Settings")
    sidecar = executable.with_name(SIDECAR)
    if not sidecar.is_file():
        raise RuntimeProblem("RUNTIME_INCOMPATIBLE", f"{executable.name} has no {SIDECAR} next to it, so it does not support Studio launches")
    if sidecar.stat().st_size > MAX_SIDECAR:
        raise RuntimeProblem("RUNTIME_INCOMPATIBLE", f"{SIDECAR} is larger than {MAX_SIDECAR} bytes")
    try:
        data = json.loads(sidecar.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise RuntimeProblem("RUNTIME_INCOMPATIBLE", f"{SIDECAR} cannot be read: {error}") from error
    problems = capability_problems(data)
    if problems:
        raise RuntimeProblem("RUNTIME_INCOMPATIBLE", "; ".join(problems))
    return data


def capability_problems(data) -> list[str]:
    from .package import PACKAGE_VERSION

    if not isinstance(data, dict) or data.get("schema") != CAPABILITIES_SCHEMA:
        return [f"not a {CAPABILITIES_SCHEMA} file"]
    out = []
    version = data.get("schema_version")
    if not isinstance(version, int) or isinstance(version, bool) or version != CAPABILITIES_VERSION:
        out.append(f"capabilities version {version!r} is not {CAPABILITIES_VERSION}; update Studio or the runtime")
    runtime = data.get("runtime")
    if not isinstance(runtime, dict) or not isinstance(runtime.get("name"), str) or not isinstance(runtime.get("version"), str):
        out.append("the runtime's name and version are missing")
    if LAUNCH_VERSION not in (data.get("launch_request_versions") or []):
        out.append(f"the runtime does not accept launch request version {LAUNCH_VERSION}")
    if PACKAGE_VERSION not in (data.get("package_schema_versions") or []):
        out.append(f"the runtime does not load package version {PACKAGE_VERSION}")
    if not isinstance(data.get("features"), list) or not all(isinstance(f, str) for f in data["features"]):
        out.append("features must be a list of names")
    for name in ("bot_slots", "teams"):
        if name in data and (not isinstance(data[name], int) or isinstance(data[name], bool) or not 0 <= data[name] <= 64):
            out.append(f"{name} must be a number 0-64")
    if not set(data.get("capture_modes") or ["none"]) <= set(CAPTURE_MODES):
        out.append(f"capture modes must be among {', '.join(CAPTURE_MODES)}")
    return out


def playability(required: dict[str, list[str]], capabilities: dict | None) -> dict:
    """{"status": "unverified" | "supported" | "unsupported", "runtime", "missing": [feature], "required": [feature]}."""

    names = sorted(required)
    if capabilities is None:
        return {"status": "unverified", "runtime": None, "missing": [], "required": names,
                "note": "no PC runtime declared which of these features it plays; Studio only checked that the content is valid"}
    declared = set(capabilities.get("features") or [])
    missing = [name for name in names if name not in declared]
    runtime = capabilities.get("runtime") or {}
    return {"status": "unsupported" if missing else "supported", "runtime": {"name": runtime.get("name"), "version": runtime.get("version")},
            "missing": missing, "required": names}


# ------------------------------------------------------------------ the request --

def sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def rom_sha256(path: Path) -> str:
    """SHA-256 of a cartridge in big-endian (.z64) byte order, as the Source Manager identifies it."""

    from .sources import normalise_n64

    data, _ = normalise_n64(Path(path).read_bytes())
    return hashlib.sha256(data).hexdigest()


def validate_request(request) -> list[str]:
    """Schema problems of a launch request (shared with runtimes that want to check what they receive)."""

    if not isinstance(request, dict) or request.get("schema") != LAUNCH_SCHEMA:
        return [f"not a {LAUNCH_SCHEMA}"]
    out = []
    if request.get("schema_version") != LAUNCH_VERSION:
        out.append(f"schema_version must be {LAUNCH_VERSION}")
    if not isinstance(request.get("session_id"), str) or not re.fullmatch(r"[0-9a-f]{32}", request["session_id"]):
        out.append("session_id must be 32 lowercase hex digits")
    package = request.get("package")
    if not isinstance(package, dict) or not isinstance(package.get("path"), str) or not Path(package["path"]).is_absolute() \
            or not HEX64.match(str(package.get("sha256"))) or not isinstance(package.get("size"), int):
        out.append("package needs an absolute path, its sha256 and size")
    if not isinstance(request.get("map"), str) or not request["map"]:
        out.append("map must name a map of the package")
    for name in ("scenario", "player_spawn"):
        if name not in request or (request[name] is not None and not isinstance(request[name], str)):
            out.append(f"{name} must be present: text or null")
    if not isinstance(request.get("studio"), dict) or not isinstance(request["studio"].get("version"), str):
        out.append("studio must carry the Studio version")
    slots = request.get("bot_slots")
    if not isinstance(slots, list) or not all(isinstance(s, int) and not isinstance(s, bool) and 0 <= s < 64 for s in slots) \
            or len(set(slots)) != len(slots):
        out.append("bot_slots must be distinct slot numbers")
    capture = request.get("capture")
    if not isinstance(capture, dict) or capture.get("mode") not in CAPTURE_MODES or (
            capture.get("mode") != "none" and not (isinstance(capture.get("directory"), str) and Path(capture["directory"]).is_absolute())):
        out.append("capture needs a mode and, unless it is none, an absolute directory")
    rom = request.get("rom")
    if not isinstance(rom, dict) or not isinstance(rom.get("path"), str) or not Path(rom["path"]).is_absolute() \
            or not HEX64.match(str(rom.get("sha256"))):
        out.append("rom needs the local cartridge's absolute path and sha256")
    if not isinstance(request.get("features"), list):
        out.append("features must be a list")
    return out


@dataclass
class LaunchPlan:
    request: dict | None
    problems: list[tuple[str, str]] = field(default_factory=list)  # (code, message)
    capabilities: dict | None = None

    @property
    def ok(self) -> bool:
        return self.request is not None and not self.problems


@dataclass
class ExportRecord:
    """What Studio remembers about the last package it wrote (in memory and in the user's local data, never in the project)."""

    path: str
    sha256: str
    size: int
    content_sha256: str  # hash of the project content when the package was written


def content_sha256(project) -> str:
    from .project import _content, dumps

    return hashlib.sha256(_content(json.loads(dumps(project))).encode("utf-8")).hexdigest()


def preflight(project, export: ExportRecord | None, executable, rom_path, map_key: str | None, scenario_key: str | None,
              player_spawn: str | None, bot_slots: list[int], capture_mode: str = "none", capture_directory=None,
              documents: dict | None = None, studio_version: str = "0", current_content: str | None = None) -> LaunchPlan:
    problems: list[tuple[str, str]] = []
    capabilities = None
    try:
        capabilities = read_capabilities(executable)
    except RuntimeProblem as error:
        problems.append((error.code, str(error)))
    package_hash = None
    if export is None or not Path(export.path).is_file():
        problems.append(("PACKAGE_MISSING", "export the project as a content package first (File > Export package)"))
    else:
        package_hash = sha256_path(Path(export.path))
        if package_hash != export.sha256:
            problems.append(("PACKAGE_STALE", "the package file changed since Studio wrote it; export it again"))
        elif (current_content or content_sha256(project)) != export.content_sha256:
            problems.append(("PACKAGE_STALE", "the project changed since the package was exported; export it again so the game sees your edits"))
    sources = [s for s in project.dependencies.get("sources", []) if s.get("platform") == "n64_btga"]
    if not rom_path:
        problems.append(("ROM_MISSING", "choose your Global Assault cartridge file in Settings > Test in Game"))
    elif not Path(rom_path).is_file():
        problems.append(("ROM_MISSING", f"the configured cartridge file {Path(rom_path).name} is not there any more"))
    else:
        wanted = {s.get("rom_sha256") for s in sources if s.get("rom_sha256")}
        actual = rom_sha256(Path(rom_path))
        if wanted and actual not in wanted:
            problems.append(("ROM_MISMATCH", "the configured cartridge is not the one this project's content comes from"))
    if map_key not in project.maps:
        problems.append(("INVALID_SELECTION", "choose a map of this project"))
    if scenario_key is not None and (scenario_key not in project.scenarios or project.scenarios[scenario_key].get("map") != map_key):
        problems.append(("INVALID_SELECTION", "the chosen scenario does not belong to the chosen map"))
    document = (documents or {}).get(map_key)
    if document is not None:
        markers = {record.get("id"): record for _, record in document.scene.records("markers") if not record.get("disabled")}
        if player_spawn is not None:
            spawn = markers.get(player_spawn)
            if spawn is None or spawn.get("category") not in ("player_spawn", "team_start"):
                problems.append(("INVALID_SELECTION", "the chosen player start is not an enabled spawn on this map"))
        available_slots = {bots_module.value(record, "ai_slot") for record in markers.values()
                           if record.get("category") == "bot_slot"}
        unavailable = sorted(set(bot_slots) - available_slots)
        if unavailable:
            problems.append(("INVALID_SELECTION", "the chosen AI slot(s) are not enabled bot placements on this map: "
                                                    + ", ".join(map(str, unavailable))))
    elif player_spawn is not None or bot_slots:
        problems.append(("INVALID_SELECTION", "open the chosen map before selecting its player start or bots"))
    if capture_mode not in CAPTURE_MODES:
        problems.append(("INVALID_SELECTION", f"capture mode must be one of {', '.join(CAPTURE_MODES)}"))
    elif capabilities is not None and capture_mode not in (capabilities.get("capture_modes") or ["none"]):
        problems.append(("CONTENT_REJECTED", f"the runtime does not offer capture mode {capture_mode}"))
    required = project_features(project, documents or {})
    if capabilities is not None:
        report = playability(required, capabilities)
        if report["missing"]:
            shown = ", ".join(report["missing"][:8]) + (" ..." if len(report["missing"]) > 8 else "")
            problems.append(("CONTENT_REJECTED", f"the runtime {report['runtime']['name']} {report['runtime']['version']} cannot play "
                                                 f"{len(report['missing'])} feature(s) this project uses: {shown}"))
        limit = capabilities.get("bot_slots", 0)
        if any(slot >= limit for slot in bot_slots):
            problems.append(("CONTENT_REJECTED", f"the runtime offers {limit} AI slot(s); the request uses slot {max(bot_slots)}"))
    if problems:
        return LaunchPlan(None, problems, capabilities)
    request = {
        "schema": LAUNCH_SCHEMA, "schema_version": LAUNCH_VERSION, "session_id": secrets.token_hex(16),
        "package": {"path": str(Path(export.path).resolve()), "sha256": package_hash, "size": Path(export.path).stat().st_size},
        "map": map_key, "scenario": scenario_key, "player_spawn": player_spawn, "bot_slots": sorted(bot_slots),
        "capture": {"mode": capture_mode, "directory": str(Path(capture_directory).resolve()) if capture_directory else None},
        "rom": {"path": str(Path(rom_path).resolve()), "sha256": rom_sha256(Path(rom_path))},
        "studio": {"version": studio_version}, "features": sorted(required),
    }
    schema_problems = validate_request(request)
    if schema_problems:
        return LaunchPlan(None, [("INVALID_SELECTION", "; ".join(schema_problems))], capabilities)
    return LaunchPlan(request, [], capabilities)


def launch(plan: LaunchPlan, executable, popen=subprocess.Popen):
    """Start the runtime with the request on its standard input.  Refuses a plan with problems; never falls back to anything else."""

    if not plan.ok:
        raise RuntimeProblem(plan.problems[0][0] if plan.problems else "INVALID_SELECTION", "the launch was not started: " +
                             "; ".join(message for _, message in plan.problems))
    process = popen([str(executable), LAUNCH_ARGUMENT, "-"], stdin=subprocess.PIPE, cwd=str(Path(executable).parent))
    try:
        process.stdin.write(json.dumps(plan.request, sort_keys=True).encode("utf-8"))
        process.stdin.close()
    except (OSError, ValueError) as error:
        raise RuntimeProblem("RUNTIME_ABSENT", f"the runtime closed before it read the request: {error}") from error
    return process
