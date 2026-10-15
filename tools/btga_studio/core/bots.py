"""Bot slots: computer-controlled tanks and Queenlords placed on a map.

A bot is a ``bot_slot`` record (Studio template ``studio_bot``) placed where it
starts.  Its typed fields:

    ai_slot           AI slot it claims (one bot per slot per map)
    kind              0 tank, 1 Queenlord
    model             content ID of the model it is drawn with (game, Studio or imported)
    team              team it fights for
    role              0 attack, 1 defend, 2 patrol, 3 capture, 4 escort, 5 support, 6 boss
    aggression        0-100
    patrol_start      navigation node its patrol route starts from (patrol)
    guard_target      record it guards (defend)
    capture_target    zone it captures (capture)
    escort_target     bot or record it escorts (escort / support)
    respawn           0 never, 1 after a delay, 2 with its wave
    respawn_seconds   delay for respawn = 1
    spawn_delay_seconds  seconds after the start before it appears
    capture_behavior  0 ignore zones, 1 capture and hold, 2 contest only
    allowed_weapons   bitmask over core.actions.WEAPONS

``check_bots`` reports duplicate slot claims, slots beyond what the runtime
declares, missing or unresolved models, invalid teams, role targets that are
missing or of the wrong kind, patrol routes that go nowhere and targets the
navigation graph cannot reach.  Studio records what a bot should do; how a
bot behaves in play is decided by the runtime, which must declare
``bot.role.<role>`` to run it.
"""

from __future__ import annotations

from .actions import WEAPONS
from .navgraph import NavGraph
from .scenario import Problem

ROLES = ("attack", "defend", "patrol", "capture", "escort", "support", "boss")
KINDS = ("tank", "queenlord")
RESPAWN = ("never", "after a delay", "with its wave")
CAPTURE = ("ignore zones", "capture and hold", "contest only")
STUDIO_SLOT_LIMIT = 16
QUEENLORD_ROLES = {"defend", "escort", "support", "boss"}
MOVING_ROLES = {"attack", "patrol", "capture", "escort", "support", "boss"}
GUARD_CATEGORIES = ("capture_zone", "trigger_volume", "team_start", "multiplayer_structure", "destructible_target", "bot_slot", "item_pickup")


def value(record: dict, name: str, default=None):
    return ((record.get("fields") or {}).get(name) or {}).get("value", default)


def role_name(record: dict) -> str:
    role = value(record, "role", 0)
    return ROLES[role] if isinstance(role, int) and 0 <= role < len(ROLES) else f"role {role}"


def kind_name(record: dict) -> str:
    kind = value(record, "kind", 0)
    return KINDS[kind] if isinstance(kind, int) and 0 <= kind < len(KINDS) else f"kind {kind}"


def bots_of(scene) -> list[dict]:
    return [r for _, r in scene.records("markers") if r.get("category") == "bot_slot" and not r.get("disabled")]


def check_bots(document, library=None, profile: dict | None = None) -> list[Problem]:
    """Problems with the bots on the open map ``document`` (``profile``: the runtime's declared limits, if configured)."""

    scene = document.scene
    bots = bots_of(scene)
    out: list[Problem] = []
    if not bots:
        return out
    slot_limit = int((profile or {}).get("bot_slots", STUDIO_SLOT_LIMIT))
    team_limit = int((profile or {}).get("teams", 4))
    records = {r["id"]: r for _, r in scene.records()}
    graph = NavGraph.from_scene(scene)
    claims: dict[int, list[str]] = {}
    for bot in bots:
        rid = bot["id"]
        slot = value(bot, "ai_slot")
        claims.setdefault(slot, []).append(rid)
        if not isinstance(slot, int) or not 0 <= slot < slot_limit:
            source = "the configured runtime declares" if profile else "Studio allows (no runtime declared its limit)"
            out.append(Problem("error", "BOT_SLOT_UNAVAILABLE", rid, "ai_slot", f"slot {slot} is beyond the {slot_limit} AI slots {source}"))
        team = value(bot, "team")
        if not isinstance(team, int) or not 0 <= team < team_limit:
            out.append(Problem("error", "INVALID_TEAM", rid, "team", f"team {team} is not one of the {team_limit} teams"))
        model = value(bot, "model")
        if not model:
            if value(bot, "kind") == 1:
                out.append(Problem("error", "QUEENLORD_UNRESOLVED", rid, "model",
                                   "no Queenlord model is resolved: none of the loaded catalogs has a model identified as a Queenlord; "
                                   "choose one or leave this Queenlord out of the package"))
            else:
                out.append(Problem("error", "MISSING_MODEL", rid, "model", "choose the model this bot is drawn with"))
        elif library is not None and model not in library._models:
            code = "MISSING_IMPORT" if str(model).startswith("user:") else "MISSING_MODEL"
            out.append(Problem("error", code, rid, "model", f"model {model} is not in any loaded catalog; register the game it comes from"))
        role = role_name(bot)
        if role not in ROLES:
            out.append(Problem("error", "INVALID_FIELD", rid, "role", f"unknown behaviour {role}"))
            continue
        if value(bot, "kind") == 1 and role not in QUEENLORD_ROLES:
            out.append(Problem("error", "INVALID_FIELD", rid, "role",
                               f"a Queenlord can be a boss or an allied defender, escort or support, not '{role}'"))
        weapons = value(bot, "allowed_weapons", 0)
        if not isinstance(weapons, int) or weapons <= 0 or weapons >> len(WEAPONS):
            out.append(Problem("error", "INVALID_FIELD", rid, "allowed_weapons", "allow at least one weapon"))
        if value(bot, "respawn") == 1 and not (isinstance(value(bot, "respawn_seconds"), int) and value(bot, "respawn_seconds") > 0):
            out.append(Problem("error", "INVALID_FIELD", rid, "respawn_seconds", "set how long the bot waits before it respawns"))
        target_field, categories = {"patrol": ("patrol_start", ("navigation_node",)), "defend": ("guard_target", GUARD_CATEGORIES),
                                    "capture": ("capture_target", ("capture_zone",)), "escort": ("escort_target", ("bot_slot",) + GUARD_CATEGORIES),
                                    "support": ("escort_target", ("bot_slot",))}.get(role, (None, ()))
        target = None
        if target_field:
            target_id = value(bot, target_field)
            target = records.get(target_id) if target_id else None
            if not target_id:
                if role != "support":
                    out.append(Problem("error", "MISSING_REFERENCE", rid, target_field,
                                       f"a {role} bot needs its {target_field.replace('_', ' ')}; pick it in the Bot panel"))
            elif target is None:
                out.append(Problem("error", "DANGLING_REFERENCE", rid, target_field, f"{target_id} is not on this map"))
            elif target.get("category") not in categories:
                out.append(Problem("error", "INVALID_REFERENCE", rid, target_field,
                                   f"{target_id} is a {target.get('category')}, not a {' or '.join(c.replace('_', ' ') for c in categories[:3])}"))
                target = None
            elif target_id == rid:
                out.append(Problem("error", "INVALID_REFERENCE", rid, target_field, "a bot cannot target itself"))
                target = None
        if role == "patrol" and target is not None:
            if graph.source == "none":
                out.append(Problem("error", "BAD_PATROL", rid, "patrol_start", "the map has no navigation; place navigation points first"))
            elif graph.source == "studio":
                route = graph.patrol_route(target["id"])
                if len(set(route)) < 2:
                    out.append(Problem("error", "BAD_PATROL", rid, "patrol_start",
                                       "the patrol route goes nowhere: link this navigation point to at least one other"))
        if role in MOVING_ROLES or target is not None:
            if not graph:
                if role in MOVING_ROLES:
                    out.append(Problem("error", "NO_NAVIGATION", rid, "role",
                                       f"a {role} bot needs navigation points to move along; this map has none"))
            elif target is not None:
                a = graph.nearest(bot["translation"][0], bot["translation"][2])
                b = graph.nearest(target["translation"][0], target["translation"][2])
                if a and b and not graph.connected(a, b):
                    out.append(Problem("error", "UNREACHABLE_TARGET", rid, target_field,
                                       "no navigation path links this bot's start to its target"))
    for slot, ids in sorted(claims.items(), key=lambda item: str(item[0])):
        if len(ids) > 1:
            for rid in ids:
                out.append(Problem("error", "DUPLICATE_SLOT", rid, "ai_slot", f"slot {slot} is claimed by {len(ids)} bots: {', '.join(sorted(ids))}"))
    return out


def next_free_slot(scene, limit: int = STUDIO_SLOT_LIMIT) -> int | None:
    used = {value(r, "ai_slot") for r in bots_of(scene)}
    return next((slot for slot in range(limit) if slot not in used), None)


def required_features(bot: dict) -> list[str]:
    return sorted({"bot", f"bot.role.{role_name(bot)}", f"bot.kind.{kind_name(bot)}"}
                  | ({"bot.respawn"} if value(bot, "respawn", 0) else set()))
