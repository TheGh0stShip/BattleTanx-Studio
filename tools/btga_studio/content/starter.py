"""ROM-free starter content: procedural placeholder models, textures and gameplay templates.

Everything here is generated from geometric primitives and arithmetic
patterns.  Nothing is traced, copied or derived from either game, so the
generated catalog may ship with Studio and lets the editor run without any
ROM.  Generation is deterministic: the same STARTER_VERSION always yields
byte-identical files and the same content-derived IDs.

    python -m tools.btga_studio.content.starter OUTPUT_DIR
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.btga_assets import export as ex  # noqa: E402
from tools.btga_assets import mesh as meshmod  # noqa: E402
from tools.btga_assets.semantics import SCENE_SCHEMA_VERSION, field, marker  # noqa: E402

STARTER_VERSION = 3  # 2 added the teleporter templates; 3 trigger volumes, zones, destructible targets, bots and item pickups
ID_VERSION = 1  # content IDs keep the version-1 seed, so existing projects still resolve every starter model and texture
PLATFORM = "studio"


def content_id(kind: str, payload) -> str:
    text = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return f"studio:{kind}:{hashlib.sha256(f'starter{ID_VERSION}|{kind}|{text}'.encode()).hexdigest()[:24]}"


# ------------------------------------------------------------------ textures --

def _texture(name: str, size: int, pixel) -> dict:
    rgba = bytearray()
    for y in range(size):
        for x in range(size):
            rgba += bytes(pixel(x, y))
    texture = {"width": size, "height": size, "rgba": bytes(rgba), "wrap": {"s": "repeat", "t": "repeat"}, "alpha": "opaque",
               "format": "rgba8", "label": name, "source": {"platform": PLATFORM, "starter": STARTER_VERSION, "label": name}}
    texture["id"] = content_id("texture", {"name": name, "rgba": hashlib.sha256(texture["rgba"]).hexdigest()})
    return texture


def _hash_noise(x: int, y: int, seed: int) -> int:
    return hashlib.sha256(f"{seed}:{x}:{y}".encode()).digest()[0]


def starter_textures() -> dict[str, dict]:
    def checker(a, b, cell):
        return lambda x, y: a if ((x // cell) + (y // cell)) % 2 == 0 else b

    def solid(colour):
        return lambda x, y: colour

    def road(x, y):
        base = (58, 60, 64, 255)
        if 30 <= x <= 33 and (y // 8) % 2 == 0:
            return (225, 205, 80, 255)
        if x in (2, 61):
            return (210, 210, 210, 255)
        return base

    def brick(x, y):
        row = y // 8
        offset = 8 if row % 2 else 0
        if y % 8 == 0 or (x + offset) % 16 == 0:
            return (180, 175, 165, 255)
        tone = 140 + _hash_noise((x + offset) // 16, row, 1) // 8
        return (tone, 70, 55, 255)

    def concrete(x, y):
        tone = 150 + _hash_noise(x, y, 2) // 16
        return (tone, tone, tone - 4, 255)

    def grass(x, y):
        tone = _hash_noise(x, y, 3) // 10
        return (60 + tone, 110 + tone, 50, 255)

    specs = {
        "checker_grey": (64, checker((200, 200, 205, 255), (120, 120, 128, 255), 8)),
        "checker_green": (64, checker((110, 160, 90, 255), (80, 130, 70, 255), 16)),
        "grass": (64, grass), "concrete": (64, concrete), "road": (64, road), "brick": (64, brick),
        "solid_red": (4, solid((200, 50, 50, 255))), "solid_blue": (4, solid((50, 90, 210, 255))),
        "solid_yellow": (4, solid((230, 200, 40, 255))), "solid_white": (4, solid((235, 235, 235, 255))),
        "solid_orange": (4, solid((235, 130, 30, 255))), "solid_purple": (4, solid((150, 70, 190, 255))),
    }
    return {name: _texture(name, size, pixel) for name, (size, pixel) in specs.items()}


# -------------------------------------------------------------------- meshes --

def _builder_from_quads(quads: list, texture: str | None, double_sided: bool = False) -> dict:
    """quads: list of (corners[4] (x, y, z), uvs[4]) wound counter-clockwise seen from outside."""

    builder = meshmod.MeshBuilder()
    material = builder.material({"id": f"{texture}|starter", "double_sided": double_sided, "alpha_mode": "OPAQUE", "texture": texture})
    for corners, uvs in quads:
        base = len(builder.positions)
        for corner in corners:
            builder.position(corner)
        for tri in ((0, 1, 2), (0, 2, 3)):
            builder.triangle(material, [{"p": base + i, "uv": list(uvs[i]), "colour": None, "normal": None} for i in tri])
    return builder.build()


def _box_quads(lo, hi, repeat: float = 1.0) -> list:
    (x0, y0, z0), (x1, y1, z1) = lo, hi
    uv = [(0, 0), (repeat, 0), (repeat, repeat), (0, repeat)]
    return [
        ([(x0, y0, z1), (x1, y0, z1), (x1, y1, z1), (x0, y1, z1)], uv),  # +Z
        ([(x1, y0, z0), (x0, y0, z0), (x0, y1, z0), (x1, y1, z0)], uv),  # -Z
        ([(x1, y0, z1), (x1, y0, z0), (x1, y1, z0), (x1, y1, z1)], uv),  # +X
        ([(x0, y0, z0), (x0, y0, z1), (x0, y1, z1), (x0, y1, z0)], uv),  # -X
        ([(x0, y1, z1), (x1, y1, z1), (x1, y1, z0), (x0, y1, z0)], uv),  # +Y
        ([(x0, y0, z0), (x1, y0, z0), (x1, y0, z1), (x0, y0, z1)], uv),  # -Y
    ]


def _merge(*meshes: dict) -> dict:
    builder = meshmod.MeshBuilder()
    for mesh in meshes:
        base = len(builder.positions)
        for position in mesh["positions"]:
            builder.position(position)
        for triangle in mesh["triangles"]:
            material = builder.material(mesh["materials"][triangle["material"]])
            builder.triangle(material, [{**c, "p": c["p"] + base} for c in triangle["corners"]])
    return builder.build()


def _pyramid(half: float, height: float, texture: str) -> dict:
    a, b, c, d = (-half, 0, half), (half, 0, half), (half, 0, -half), (-half, 0, -half)
    top = (0, height, 0)
    quads = [([a, b, top, top], [(0, 0), (1, 0), (0.5, 1), (0.5, 1)]), ([b, c, top, top], [(0, 0), (1, 0), (0.5, 1), (0.5, 1)]),
             ([c, d, top, top], [(0, 0), (1, 0), (0.5, 1), (0.5, 1)]), ([d, a, top, top], [(0, 0), (1, 0), (0.5, 1), (0.5, 1)])]
    builder = meshmod.MeshBuilder()
    material = builder.material({"id": f"{texture}|starter", "double_sided": False, "alpha_mode": "OPAQUE", "texture": texture})
    for corners, uvs in quads:
        base = len(builder.positions)
        for corner in corners[:3]:
            builder.position(corner)
        builder.triangle(material, [{"p": base + i, "uv": list(uvs[i]), "colour": None, "normal": None} for i in range(3)])
    base = len(builder.positions)
    for corner in (a, d, c, b):
        builder.position(corner)
    for tri in ((0, 1, 2), (0, 2, 3)):
        builder.triangle(material, [{"p": base + i, "uv": [0, 0], "colour": None, "normal": None} for i in tri])
    return builder.build()


def starter_meshes(textures: dict[str, dict]) -> dict[str, tuple[dict, str]]:
    """name -> (mesh, category)."""

    t = {name: texture["id"] for name, texture in textures.items()}
    ramp_quads = [
        ([(-100, 0, 100), (100, 0, 100), (100, 100, -100), (-100, 100, -100)], [(0, 0), (2, 0), (2, 2), (0, 2)]),
        ([(100, 0, 100), (100, 0, -100), (100, 100, -100), (100, 100, -100)], [(0, 0), (1, 0), (1, 1), (1, 1)]),
        ([(-100, 0, -100), (-100, 0, 100), (-100, 100, -100), (-100, 100, -100)], [(0, 0), (1, 0), (1, 1), (1, 1)]),
        ([(100, 0, -100), (-100, 0, -100), (-100, 100, -100), (100, 100, -100)], [(0, 0), (1, 0), (1, 1), (0, 1)]),
    ]
    flag = _merge(_builder_from_quads(_box_quads((-4, 0, -4), (4, 160, 4)), t["solid_white"]),
                  _builder_from_quads([([(4, 110, 0), (64, 110, 0), (64, 155, 0), (4, 155, 0)], [(0, 0), (1, 0), (1, 1), (0, 1)])], t["solid_yellow"], True))
    spawn = _merge(_builder_from_quads(_box_quads((-30, 0, -30), (30, 6, 30)), t["solid_blue"]), _pyramid(14, 60, t["solid_blue"]))
    pickup = _merge(_pyramid(24, 30, t["solid_orange"]))
    hazard = _merge(_builder_from_quads(_box_quads((-40, 0, -40), (40, 10, 40)), t["solid_red"]), _pyramid(20, 70, t["solid_red"]))
    teleporter = _merge(_builder_from_quads(_box_quads((-60, 0, -60), (60, 4, 60)), t["solid_purple"]),
                        _builder_from_quads(_box_quads((-40, 4, -40), (40, 8, 40)), t["solid_white"]),
                        *[_builder_from_quads(_box_quads((x - 5, 0, z - 5), (x + 5, 90, z + 5)), t["solid_purple"])
                          for x, z in ((-55, -55), (55, -55), (55, 55), (-55, 55))])
    teleport_exit = _merge(_builder_from_quads(_box_quads((-40, 0, -40), (40, 4, 40)), t["solid_purple"]),
                           _builder_from_quads([([(-25, 6, 20), (25, 6, 20), (0, 6, -45), (0, 6, -45)], [(0, 0), (1, 0), (0.5, 1), (0.5, 1)])],
                                               t["solid_white"], True))
    tank = _merge(_builder_from_quads(_box_quads((-35, 0, -55), (35, 30, 55)), t["checker_grey"]),
                  _builder_from_quads(_box_quads((-22, 30, -20), (22, 50, 22)), t["concrete"]),
                  _builder_from_quads(_box_quads((-4, 38, -90), (4, 44, -20)), t["concrete"]))
    trigger = _merge(*[_builder_from_quads(_box_quads((x - 4, 0, z - 4), (x + 4, 60, z + 4)), t["solid_yellow"])
                       for x, z in ((-50, -50), (50, -50), (50, 50), (-50, 50))],
                     _builder_from_quads(_box_quads((-54, 56, -54), (54, 60, 54)), t["solid_yellow"]))
    capture = _merge(_builder_from_quads(_box_quads((-70, 0, -70), (70, 3, 70)), t["solid_blue"]), flag)
    destructible = _merge(_builder_from_quads(_box_quads((-30, 0, -30), (30, 60, 30)), t["solid_orange"]),
                          _builder_from_quads(_box_quads((-32, 26, -32), (32, 34, 32)), t["checker_grey"]))
    item = _merge(_pyramid(22, 34, t["solid_white"]), _builder_from_quads(_box_quads((-26, 0, -26), (26, 4, 26)), t["checker_green"]))
    return {
        "marker_trigger": (trigger, "Markers"),
        "marker_capture": (capture, "Markers"),
        "marker_destructible": (destructible, "Markers"),
        "marker_item": (item, "Markers"),
        "ground_tile": (_builder_from_quads([([(-256, 0, 256), (256, 0, 256), (256, 0, -256), (-256, 0, -256)], [(0, 0), (8, 0), (8, 8), (0, 8)])], t["grass"], True), "Terrain"),
        "ground_tile_concrete": (_builder_from_quads([([(-256, 0, 256), (256, 0, 256), (256, 0, -256), (-256, 0, -256)], [(0, 0), (4, 0), (4, 4), (0, 4)])], t["concrete"], True), "Terrain"),
        "road_straight": (_builder_from_quads([([(-64, 0.5, 256), (64, 0.5, 256), (64, 0.5, -256), (-64, 0.5, -256)], [(0, 0), (1, 0), (1, 4), (0, 4)])], t["road"], True), "Roads"),
        "cube": (_builder_from_quads(_box_quads((-50, 0, -50), (50, 100, 50)), t["checker_grey"]), "Primitives"),
        "ramp": (_builder_from_quads(ramp_quads, t["concrete"]), "Primitives"),
        "wall": (_builder_from_quads(_box_quads((-200, 0, -10), (200, 150, 10), 2.0), t["brick"]), "Structures"),
        "building_small": (_builder_from_quads(_box_quads((-120, 0, -120), (120, 220, 120), 2.0), t["brick"]), "Structures"),
        "building_tall": (_builder_from_quads(_box_quads((-90, 0, -90), (90, 640, 90), 3.0), t["concrete"]), "Structures"),
        "marker_spawn": (spawn, "Markers"),
        "marker_pickup": (pickup, "Markers"),
        "marker_hazard": (hazard, "Markers"),
        "marker_objective": (flag, "Markers"),
        "tank_placeholder": (tank, "Vehicles"),
        "marker_teleporter": (teleporter, "Markers"),
        "marker_teleport_exit": (teleport_exit, "Markers"),
    }


def starter_models(textures: dict[str, dict]) -> dict[str, dict]:
    out = {}
    for name, (mesh, category) in sorted(starter_meshes(textures).items()):
        model_id = content_id("model", {"name": name, "mesh": mesh})
        out[name] = {"id": model_id, "schema": "btga-neutral", "schema_version": 1, "kind": "model",
                     "source": {"platform": PLATFORM, "starter": STARTER_VERSION, "label": name, "library": f"starter:{category}"},
                     "mesh": mesh, "bounds": meshmod.bounds(mesh)}
    return out


# ----------------------------------------------------------------- gameplay --

def _studio(value, kind, meaning, **options):
    return field(value, kind, None, "proven", f"Studio format: {meaning}", **options)


TEAM = (0, 7)


def gameplay_templates(models: dict[str, dict]) -> list[dict]:
    """Studio-native typed records (target: the PC-port content package)."""

    def template(name, category, fields, model, restrictions=None):
        return marker(f"studio:template:{name}", PLATFORM, f"studio_{name}", category, [0.0, 0.0, 0.0], 0, fields,
                      source={"starter": STARTER_VERSION, "template": True}, evidence=["Studio-defined record"], confidence="proven",
                      dependencies=[{"kind": "model", "field": "display_model", "id": models[model]["id"], "status": "resolved"}],
                      models=[models[model]["id"]], restrictions=restrictions or {})

    unit_types = {i: f"unit type {i}" for i in range(15)}
    return [
        template("player_spawn", "player_spawn", {"team": _studio(0, "u8", "team", legal=TEAM), "player": _studio(0, "u8", "player slot", legal=(0, 3))},
                 "marker_spawn"),
        template("team_start", "team_start", {"team": _studio(0, "u8", "team", legal=TEAM)}, "marker_spawn", {"max_per_team": 5}),
        template("unit_generator", "unit_spawner", {
            "team": _studio(1, "u8", "owning team", legal=TEAM),
            "unit_type_mask": _studio(1, "u16", "unit types to cycle through", legal=(1, 0x7FFF), bits=15),
            "spawn_budget": _studio(10, "u16", "units to spawn", sentinel={0: "disabled", 0xFFFF: "unlimited"}),
            "spawn_interval": _studio(300, "u32", "ticks between spawns", legal=(1, 1_000_000)),
            "max_spawned": _studio(4, "u8", "maximum alive at once", legal=(1, 255)),
        }, "marker_spawn"),
        template("pickup", "pickup", {
            "pickup_type": _studio(0, "u8", "pickup type", enum={0: "repair", 1: "ammunition", 2: "weapon", 3: "boost"}, closed=True),
            "respawn_seconds": _studio(30, "u16", "respawn delay (0 = never)"),
        }, "marker_pickup"),
        template("hazard", "hazard", {"radius": _studio(100, "u16", "damage radius", legal=(1, 4000)),
                                      "damage": _studio(10, "u8", "damage per second", legal=(1, 255))}, "marker_hazard"),
        template("objective", "objective", {
            "objective_type": _studio(0, "u8", "objective type", enum={0: "destroy target", 1: "reach area", 2: "defend area", 3: "collect"}, closed=True),
            "target": _studio(None, "ref", "record the objective refers to"),
            "required": _studio(1, "u8", "count required", legal=(1, 255)),
            "label": _studio("Objective", "text", "label shown to the player", legal=(0, 64)),
        }, "marker_objective"),
        template("teleporter", "teleporter", {
            "exit": _studio(None, "ref", "teleport exit vehicles arrive at"),
            "radius": _studio(90, "u16", "trigger radius", legal=(20, 1000)),
            "cooldown_seconds": _studio(2, "u8", "seconds before the same vehicle can use it again", legal=(0, 60)),
            "keep_speed": _studio(0, "u8", "speed after arriving", enum={0: "stop at the exit", 1: "keep speed"}, closed=True),
        }, "marker_teleporter"),
        template("teleport_exit", "teleport_exit", {"label": _studio("Teleport exit", "text", "name shown in the editor", legal=(0, 64))},
                 "marker_teleport_exit"),
        template("nav_node", "navigation_node", {f"link_{i}": _studio(None, "ref", f"linked navigation node {i}") for i in range(4)}, "marker_pickup"),
        template("trigger_volume", "trigger_volume", {
            "shape": _studio(0, "u8", "volume shape", enum={0: "circle", 1: "box"}, closed=True),
            "radius": _studio(150, "u16", "circle radius (world units)", legal=(10, 8000)),
            "half_x": _studio(150, "u16", "box half-width across (world units)", legal=(10, 8000)),
            "half_z": _studio(150, "u16", "box half-depth (world units)", legal=(10, 8000)),
            "height": _studio(400, "u16", "height above its base (world units)", legal=(10, 16000)),
            "label": _studio("Trigger volume", "text", "name shown in the editor and scenario graph", legal=(0, 48)),
        }, "marker_trigger"),
        template("capture_zone", "capture_zone", {
            "zone_mode": _studio(0, "u8", "what the zone is for", enum={0: "capture", 1: "defend", 2: "king of the hill"}, closed=True),
            "radius": _studio(250, "u16", "zone radius (world units)", legal=(20, 8000)),
            "owner": _studio(255, "u8", "team owning it at the start", legal=TEAM, sentinel={255: "neutral"}),
            "label": _studio("Zone", "text", "name shown in the editor and scenario graph", legal=(0, 48)),
        }, "marker_capture"),
        template("destructible_target", "destructible_target", {
            "health": _studio(500, "u16", "hit points", legal=(1, 60000)),
            "owner": _studio(255, "u8", "team it belongs to", legal=TEAM, sentinel={255: "nobody"}),
            "score": _studio(250, "u16", "points for destroying it", legal=(0, 60000)),
            "respawn_seconds": _studio(0, "u16", "seconds until it is rebuilt (0 = never)", legal=(0, 3600)),
            "model": _studio("", "text", "content ID of the intact model (empty: the editor marker)", legal=(0, 96)),
            "destroyed_model": _studio("", "text", "content ID of the wreck (empty: nothing remains)", legal=(0, 96)),
            "label": _studio("Target", "text", "name shown in the editor and scenario graph", legal=(0, 48)),
        }, "marker_destructible"),
        template("bot", "bot_slot", {
            "ai_slot": _studio(0, "u8", "AI slot the bot claims", legal=(0, 63)),
            "kind": _studio(0, "u8", "what it is", enum={0: "tank", 1: "Queenlord"}, closed=True),
            "model": _studio("", "text", "content ID of the model it is drawn with", legal=(0, 96)),
            "team": _studio(1, "u8", "team", legal=TEAM),
            "role": _studio(0, "u8", "behaviour", enum={0: "attack", 1: "defend", 2: "patrol", 3: "capture", 4: "escort", 5: "support", 6: "boss"},
                            closed=True),
            "aggression": _studio(50, "u8", "aggression (0 cautious - 100 reckless)", legal=(0, 100)),
            "patrol_start": _studio(None, "ref", "navigation point its patrol starts from"),
            "guard_target": _studio(None, "ref", "what it defends"),
            "capture_target": _studio(None, "ref", "zone it captures"),
            "escort_target": _studio(None, "ref", "bot or object it escorts or supports"),
            "respawn": _studio(0, "u8", "respawn", enum={0: "never", 1: "after a delay", 2: "with its wave"}, closed=True),
            "respawn_seconds": _studio(20, "u16", "seconds before it respawns", legal=(0, 3600)),
            "spawn_delay_seconds": _studio(0, "u16", "seconds after the start before it appears", legal=(0, 3600)),
            "capture_behavior": _studio(0, "u8", "what it does with zones", enum={0: "ignore zones", 1: "capture and hold", 2: "contest only"},
                                        closed=True),
            "allowed_weapons": _studio(63, "u8", "weapons it may use (cannon, machine gun, rockets, guided missiles, mines, special)",
                                       legal=(1, 63), bits=6),
        }, "tank_placeholder"),
        template("item_pickup", "item_pickup", {
            "item": _studio("", "text", "item definition this pickup gives", legal=(0, 48)),
            "quantity": _studio(1, "u8", "how many it gives each time", legal=(1, 99)),
            "respawn_seconds": _studio(30, "u16", "seconds until it comes back (0 = never)", legal=(0, 3600)),
            "uses": _studio(0, "u16", "times it can be picked up", legal=(1, 999), sentinel={0: "unlimited"}),
            "team": _studio(255, "u8", "team allowed to pick it up", legal=TEAM, sentinel={255: "any team"}),
        }, "marker_item"),
    ]


# ------------------------------------------------------------------- catalog --

def template_scene(models: dict[str, dict], textures: dict[str, dict]) -> dict:
    """A small showcase map holding one of each template; it doubles as the template source."""

    templates = gameplay_templates(models)
    instances = []
    for index, (name, x, z) in enumerate((("ground_tile", 0, 0), ("ground_tile", 512, 0), ("ground_tile", 0, 512), ("ground_tile", 512, 512),
                                          ("road_straight", 256, 256), ("building_small", 600, 120), ("building_tall", 120, 620),
                                          ("wall", 256, -180), ("ramp", 420, 420), ("cube", 80, 120))):
        instances.append({"id": f"studio:placement:showcase:{index}", "model": models[name]["id"], "translation": [float(x), 0.0, float(z)],
                          "yaw": 0, "category": "model"})
    markers = []
    for index, record in enumerate(templates):
        placed = dict(record)
        placed["translation"] = [float(-200 + 100 * index), 0.0, -300.0]
        markers.append(placed)
    return {
        "id": "studio:scene:starter_showcase", "schema": "btga-neutral", "schema_version": 1, "kind": "scene",
        "source": {"platform": PLATFORM, "starter": STARTER_VERSION},
        "semantics_version": SCENE_SCHEMA_VERSION, "instances": instances, "collision": [], "markers": markers,
        "environment": [{"kind": "studio", "clear_and_fog_colour": [96, 120, 150], "evidence": "Studio environment"}],
        "bounds": {"map": {"x": [-1024.0, 1536.0], "z": [-1024.0, 1536.0], "status": "studio", "evidence": "Studio map limits"}},
        "minimap": {"status": "generated", "selections": []}, "navigation": None, "music": {"status": "none"}, "unresolved": [],
        "models": sorted({i["model"] for i in instances}),
    }


def write_starter_catalog(root: Path) -> Path:
    exporter = ex.Exporter(Path(root))
    textures = {t["id"]: t for t in starter_textures().values()}
    models = starter_models({t["label"]: t for t in textures.values()})
    exporter.sources.append({"platform": PLATFORM, "title": f"Studio starter content v{STARTER_VERSION}", "starter": STARTER_VERSION})
    scene = template_scene(models, textures)
    exporter.add_scene(scene, {m["id"]: m for m in models.values() if m["id"] in scene["models"]}, textures,
                       {"platform": PLATFORM, "level": "starter-showcase", "name": "Starter showcase", "campaign": "Studio starter",
                        "variant": "default", "variants": ["default"]})
    for model in models.values():
        exporter.models.setdefault(model["id"], model)
    exporter.finish()
    (Path(root) / "starter.json").write_text(json.dumps({"starter_version": STARTER_VERSION,
                                                          "templates": gameplay_templates(models),
                                                          "models": {name: model["id"] for name, model in sorted(models.items())}},
                                                         sort_keys=True, indent=1) + "\n", encoding="utf-8")
    return Path(root)


def ensure_starter_catalog(directory: Path) -> Path:
    """Generate the starter catalog into ``directory/v<version>`` once."""

    target = Path(directory) / f"v{STARTER_VERSION}"
    if not (target / "starter.json").is_file():
        temporary = Path(directory) / f"v{STARTER_VERSION}.partial"
        if temporary.exists():
            import shutil

            shutil.rmtree(temporary)
        write_starter_catalog(temporary)
        temporary.replace(target) if not target.exists() else None
    return target


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("output", type=Path)
    args = parser.parse_args(argv)
    write_starter_catalog(args.output)
    print(f"starter catalog v{STARTER_VERSION} written")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
