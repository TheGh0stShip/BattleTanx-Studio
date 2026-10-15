"""Custom pickups and items: reusable definitions with declarative effects.

A project keeps item definitions in ``project.items`` (key -> definition);
every map of the project can place them with ``item_pickup`` records (a
Studio template whose ``item`` field names the definition).  Definitions::

    {"title", "category", "model"?: content ID, "icon": "studio:icon:<name>" | texture content ID,
     "effects": [action], "duration_seconds", "stacking": "none" | "refresh" | "stack", "max_stack",
     "magnitude"?: number shown in the editor summary, "notes"?}

Effects use the constrained action vocabulary in ``core.actions`` (context
"item"): modify health/armor/ammunition/currency/score, grant or remove a
weapon or item, timed stat modifiers, counters, firing a trigger, completing
an objective, a message or a sound.  There is no way to embed code.

``check_item`` validates one definition; ``grant_cycle`` finds items that
give each other in a loop (a pickup that would grant forever).  The map
side (quantity, respawn, uses, team restriction on each pickup) is checked
by ``check_pickup``.  ``required_features`` names the runtime features an
item needs.
"""

from __future__ import annotations

import copy

from . import actions as act
from .scenario import Problem, next_id, requirement_cycle

CATEGORIES = ("health", "armor", "ammunition", "weapon", "currency", "score", "modifier", "key", "scripted")
STACKING = ("none", "refresh", "stack")
ICONS = ("repair", "armor", "ammo", "weapon", "cash", "star", "boost", "key", "script", "shield", "nuke", "target")
TEAMS_ANY = 255
MAX_ITEMS = 256

PRESETS = {
    "repair_kit": {"title": "Repair kit", "category": "health", "icon": "studio:icon:repair",
                   "effects": [{"do": "modify_stat", "stat": "health", "amount": 50, "mode": "add"}]},
    "armor_plate": {"title": "Armor plate", "category": "armor", "icon": "studio:icon:armor",
                    "effects": [{"do": "modify_stat", "stat": "armor", "amount": 25, "mode": "add"}]},
    "ammo_crate": {"title": "Ammunition crate", "category": "ammunition", "icon": "studio:icon:ammo",
                   "effects": [{"do": "modify_stat", "stat": "ammunition", "amount": 20, "mode": "add"}]},
    "rocket_pod": {"title": "Rocket pod", "category": "weapon", "icon": "studio:icon:weapon",
                   "effects": [{"do": "grant_weapon", "weapon": "rockets", "ammunition": 10}]},
    "cash_bundle": {"title": "Cash bundle", "category": "currency", "icon": "studio:icon:cash",
                    "effects": [{"do": "modify_stat", "stat": "currency", "amount": 100, "mode": "add"}]},
    "score_star": {"title": "Score star", "category": "score", "icon": "studio:icon:star",
                   "effects": [{"do": "modify_stat", "stat": "score", "amount": 250, "mode": "add"}]},
    "speed_boost": {"title": "Speed boost", "category": "modifier", "icon": "studio:icon:boost", "duration_seconds": 15,
                    "stacking": "refresh", "effects": [{"do": "timed_modifier", "stat": "speed", "factor": 1.5, "seconds": 15}]},
    "keycard": {"title": "Keycard", "category": "key", "icon": "studio:icon:key", "effects": []},
    "alarm_switch": {"title": "Alarm switch", "category": "scripted", "icon": "studio:icon:script",
                     "effects": [{"do": "message", "text": "Alarm raised!", "seconds": 4, "audience": "everyone"}]},
}


def new_item(preset: str = "repair_kit", model: str | None = None) -> dict:
    base = copy.deepcopy(PRESETS[preset])
    return {"title": base["title"], "category": base["category"], "model": model, "icon": base["icon"], "effects": base["effects"],
            "duration_seconds": base.get("duration_seconds", 0), "stacking": base.get("stacking", "none"), "max_stack": 1}


def item_key(title: str, existing) -> str:
    """A stable key from a title (``hub.slug`` style), numbered when taken."""

    import re

    stem = re.sub(r"[^a-z0-9]+", "_", title.lower()).strip("_")[:40] or "item"
    if not stem[0].isalpha():
        stem = "item_" + stem
    return stem if stem not in existing else next_id(stem + "_", existing)


def grant_cycle(items: dict) -> list[str] | None:
    """Items that grant each other in a loop (``grant_item``), or None."""

    graph = {key: {"requires": sorted({a.get("item") for a in (item.get("effects") or []) if isinstance(a, dict)
                                       and a.get("do") == "grant_item" and a.get("item") in items})}
             for key, item in items.items() if isinstance(item, dict)}
    return requirement_cycle(graph)


def check_item(key: str, item, items: dict, known_models=None, known_textures=None) -> list[Problem]:
    target = f"items:{key}"
    out: list[Problem] = []

    def err(code, where, message, severity="error"):
        out.append(Problem(severity, code, target, where, message))

    if not isinstance(item, dict):
        return [Problem("error", "INVALID_ITEM", target, None, "an item must be an object")]
    title = item.get("title")
    if not isinstance(title, str) or not title or len(title) > 48 or any(ord(c) < 32 for c in title):
        err("INVALID_FIELD", "title", "give the item a name (at most 48 characters)")
    if item.get("category") not in CATEGORIES:
        err("INVALID_FIELD", "category", f"category must be one of {', '.join(CATEGORIES)}")
    model = item.get("model")
    if model is not None:
        if not isinstance(model, str) or not model:
            err("INVALID_FIELD", "model", "choose a model or none")
        elif known_models is not None and model not in known_models:
            code = "MISSING_IMPORT" if model.startswith("user:") else "MISSING_MODEL"
            err(code, "model", f"model {model} is not in any loaded catalog; register the game it comes from or choose another")
    icon = item.get("icon")
    if not isinstance(icon, str) or not (icon.startswith("studio:icon:") and icon.split(":", 2)[2] in ICONS
                                         or not icon.startswith("studio:icon:") and (known_textures is None or icon in known_textures)):
        err("MISSING_ICON", "icon", "choose one of Studio's icons or a picture from your library")
    duration = item.get("duration_seconds", 0)
    if not isinstance(duration, int) or isinstance(duration, bool) or not 0 <= duration <= 3600:
        err("INVALID_FIELD", "duration_seconds", "duration must be 0 (instant) to 3600 seconds")
    if item.get("stacking", "none") not in STACKING:
        err("INVALID_FIELD", "stacking", "stacking must be none, refresh or stack")
    max_stack = item.get("max_stack", 1)
    if not isinstance(max_stack, int) or isinstance(max_stack, bool) or not 1 <= max_stack <= 99:
        err("INVALID_FIELD", "max_stack", "max stack must be 1-99")
    elif item.get("stacking") != "stack" and max_stack != 1:
        err("INVALID_FIELD", "max_stack", "only stacking items can hold more than one", "warning")
    effects = item.get("effects")
    if not isinstance(effects, list):
        err("INVALID_FIELD", "effects", "effects must be a list")
        effects = []
    ctx = act.ActionContext("item", items=set(items), check_refs=False)
    for field_name, message in act.check_actions(effects, ctx, "effects"):
        code = "UNSUPPORTED_EFFECT" if "cannot be used" in message or "unknown action" in message else (
            "DANGLING_REFERENCE" if "does not exist" in message else "INVALID_FIELD")
        err(code, field_name, message)
    timed = [a for a in effects if isinstance(a, dict) and a.get("do") == "timed_modifier"]
    if item.get("category") == "modifier" and not timed:
        err("INVALID_FIELD", "effects", "a temporary modifier needs a 'change a stat for a while' effect")
    if timed and duration and any(isinstance(a.get("seconds"), int) and a["seconds"] > duration for a in timed):
        err("INVALID_FIELD", "duration_seconds", "an effect lasts longer than the item's duration")
    if item.get("category") == "weapon" and not any(isinstance(a, dict) and a.get("do") == "grant_weapon" for a in effects):
        err("INVALID_FIELD", "effects", "a weapon item needs a 'give a weapon' effect")
    for action in effects:
        if isinstance(action, dict) and action.get("do") == "grant_item" and action.get("item") == key:
            err("ITEM_CYCLE", "effects", "an item cannot give itself")
    return out


def check_items(items: dict, known_models=None, known_textures=None) -> list[Problem]:
    out = []
    if len(items) > MAX_ITEMS:
        out.append(Problem("error", "LIMIT", "items", None, f"at most {MAX_ITEMS} item definitions"))
    for key, item in sorted(items.items()):
        out += check_item(key, item, items, known_models, known_textures)
    cycle = grant_cycle(items)
    if cycle and len(cycle) > 2:
        out.append(Problem("error", "ITEM_CYCLE", f"items:{cycle[0]}", "effects", "items give each other in a loop: " + " -> ".join(cycle)))
    return out


def check_pickup(record: dict, items: dict, scenarios_on_map: list[dict]) -> list[Problem]:
    """Map-side checks of one ``item_pickup`` record against the project's items and the map's scenarios."""

    out = []
    fields = record.get("fields", {})
    key = (fields.get("item") or {}).get("value")
    if not key:
        return [Problem("error", "MISSING_REFERENCE", record["id"], "item", "choose which item this pickup gives")]
    item = items.get(key)
    if item is None:
        return [Problem("error", "DANGLING_REFERENCE", record["id"], "item", f"item {key!r} is not defined in this project")]
    triggers = set().union(*[set((s.get("triggers") or {})) for s in scenarios_on_map]) if scenarios_on_map else set()
    objectives = set().union(*[set((s.get("objectives") or {})) for s in scenarios_on_map]) if scenarios_on_map else set()
    counters = set().union(*[set((s.get("counters") or {})) for s in scenarios_on_map]) if scenarios_on_map else set()
    for number, action in enumerate(item.get("effects") or []):
        if not isinstance(action, dict):
            continue
        for family, value in act.referenced(action):
            known = {"trigger": triggers, "objective": objectives, "counter": counters}.get(family)
            if known is not None and value not in known:
                out.append(Problem("error", "DANGLING_REFERENCE", record["id"], f"item.effects[{number}]",
                                   f"item {item.get('title', key)!r} uses {family} {value!r}, which no scenario on this map defines"))
    return out


def required_features(item: dict) -> list[str]:
    features = {"item", f"item.category.{item.get('category')}", f"item.stacking.{item.get('stacking', 'none')}"}
    for action in item.get("effects") or []:
        if isinstance(action, dict) and action.get("do") in act.ACTIONS:
            features.add(act.feature(action["do"]))
    return sorted(features)


def summary(item: dict, names=None) -> str:
    effects = "; ".join(act.describe(a, names) for a in item.get("effects") or [] if isinstance(a, dict)) or "no effect"
    extra = f", lasts {item['duration_seconds']} s" if item.get("duration_seconds") else ""
    return f"{item.get('category', '?')}: {effects}{extra}"
