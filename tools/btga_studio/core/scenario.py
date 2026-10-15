"""Platform-neutral scenarios: match rules, objectives, triggers, timers, counters, waves, groups and transitions.

A project holds scenarios in ``project.scenarios`` (key -> scenario).  A
scenario belongs to one map and refers to that map's records (trigger
volumes, zones, destructible targets, bots, spawns, teleporters) by their
stable record IDs, to the project's item definitions by key, and to its own
elements by element ID.  Everything is plain JSON; keys this Studio does not
know are carried through unchanged.

    {"schema": "btga-scenario", "version": 1, "title", "map",
     "rules":   {"mode", "teams", "time_limit_seconds", "score_limit", "lives", "friendly_fire",
                 "score": {"kill", "objective", "multipliers": [{"id", "applies", "factor", "zone"?, "while_timer"?}]}},
     "start":   {"player_spawn", "transition", "briefing", "actions": [...]},
     "end":     {"victory": {"transition", "next"?, "message"}, "failure": {"transition", "message"}},
     "victory": [condition], "failure": [condition],
     "objectives": {id: {"kind", "title", "params", "requires": [id], "optional", "repeat", "hidden",
                         "on_complete": [action], "on_fail": [action]}},
     "triggers":   {id: {"title", "on", "volume"?, "subject", "objective"?, "timer"?, "counter"?, "value"?, "record"?,
                         "seconds"?, "once", "enabled", "actions": [action]}},
     "timers":     {id: {"title", "seconds", "starts", "repeat", "visible", "on_expire": [action]}},
     "counters":   {id: {"title", "initial", "min", "max", "visible"}},
     "waves":      {id: {"title", "bots": [record], "spawner"?, "count", "interval_seconds", "delay_seconds", "repeat"}},
     "groups":     {id: {"title", "members": [record], "active_at_start"}},
     "layout":     {id: [x, y]}}      # graph editor positions only

Ordered, parallel, optional and repeatable objectives are all expressed
with ``requires`` (a directed acyclic graph), ``optional`` and ``repeat``
(``1`` = once, ``n`` = n times, ``0`` = repeats for the whole match and
can therefore never be required).  Actions use ``core.actions``.

``check(key, scenario, ctx)`` returns ``Problem`` items naming the scenario,
the element and the field.  ``required_features`` lists the runtime features
a loader must declare to play the scenario; ``core.runtime`` compares them
with what a configured runtime declares.  Nothing here claims a scenario is
playable: Studio supports authoring it, the runtime decides whether it runs.
"""

from __future__ import annotations

import copy
import re
from dataclasses import asdict, dataclass, field

from . import actions as act

SCHEMA = "btga-scenario"
VERSION = 1
ELEMENT_ID = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
MAX_ELEMENTS = 256
MODES = ("mission", "deathmatch", "team_deathmatch", "capture_the_flag", "king_of_the_hill", "survival", "race", "queenlord_rescue", "custom")
TRANSITIONS_START = ("cut", "fade", "briefing", "flyover")
TRANSITIONS_VICTORY = ("results", "next_scenario", "menu")
TRANSITIONS_FAILURE = ("retry", "results", "menu")
SUBJECTS = ("player", "any_tank", "team_0", "team_1", "team_2", "team_3", "any_bot")
TRIGGER_EVENTS = ("enter", "exit", "inside", "scenario_start", "objective_completed", "objective_failed", "timer_expired",
                  "counter_reached", "destroyed")
VOLUME_EVENTS = ("enter", "exit", "inside")
SPAWN_CATEGORIES = ("player_spawn", "team_start", "team_spawn_slot")
VOLUME_CATEGORIES = ("trigger_volume", "capture_zone")
DESTRUCTIBLE_CATEGORIES = ("destructible_target", "destructible", "structure", "building", "turret", "turret_emplacement",
                           "turret_random", "multiplayer_structure", "bot_slot")

# kind -> parameter specs: ("record", categories) ("records", categories) ("int", lo, hi) ("item",) ("counter",)
# ("trigger",) ("team",) ("team_or_any",)
OBJECTIVE_KINDS: dict[str, dict] = {
    "destroy": {"params": {"targets": ("records", DESTRUCTIBLE_CATEGORIES), "count": ("int", 0, 999)},
                "label": "Destroy targets", "help": "count 0 means all of them"},
    "capture": {"params": {"zone": ("record", ("capture_zone",)), "hold_seconds": ("int", 1, 3600), "team": ("team",)},
                "label": "Capture a zone"},
    "defend": {"params": {"zone": ("record", VOLUME_CATEGORIES), "seconds": ("int", 1, 7200)}, "label": "Defend a zone"},
    "survive": {"params": {"seconds": ("int", 1, 7200)}, "label": "Survive"},
    "escort": {"params": {"escortee": ("record", ("bot_slot",)), "destination": ("record", VOLUME_CATEGORIES)}, "label": "Escort"},
    "collect": {"params": {"item": ("item",), "count": ("int", 1, 999)}, "label": "Collect items"},
    "race": {"params": {"checkpoints": ("records", ("trigger_volume",)), "laps": ("int", 1, 20)}, "label": "Race through checkpoints"},
    "kill_count": {"params": {"count": ("int", 1, 999), "team": ("team_or_any",)}, "label": "Destroy a number of tanks"},
    "boss": {"params": {"boss": ("record", ("bot_slot",))}, "label": "Defeat a boss"},
    "reach": {"params": {"area": ("record", VOLUME_CATEGORIES)}, "label": "Reach an area"},
    "counter": {"params": {"counter": ("counter",), "value": ("int", -100000, 100000)}, "label": "Bring a counter to a value"},
    "trigger": {"params": {"trigger": ("trigger",)}, "label": "Fire a trigger"},
    "marker_objective": {"params": {"marker": ("record", ("objective", "mission_trigger"))}, "label": "Map objective marker"},
}
OBJECTIVE_DEFAULTS = {
    "destroy": {"targets": [], "count": 0}, "capture": {"zone": None, "hold_seconds": 20, "team": 0},
    "defend": {"zone": None, "seconds": 120}, "survive": {"seconds": 180}, "escort": {"escortee": None, "destination": None},
    "collect": {"item": None, "count": 1}, "race": {"checkpoints": [], "laps": 1}, "kill_count": {"count": 10, "team": -1},
    "boss": {"boss": None}, "reach": {"area": None}, "counter": {"counter": None, "value": 10}, "trigger": {"trigger": None},
    "marker_objective": {"marker": None},
}
CONDITIONS = {
    "all_required_objectives": {}, "objective": {"objective": "objective"}, "score_reached": {"value": "int"},
    "last_team_standing": {}, "timer_expired": {"timer": "timer"}, "counter_reached": {"counter": "counter", "value": "int"},
    "player_destroyed": {}, "objective_failed": {"objective": "objective"}, "record_destroyed": {"record": "record"},
}
VICTORY_ONLY = {"all_required_objectives", "score_reached", "last_team_standing"}
FAILURE_ONLY = {"player_destroyed", "objective_failed", "record_destroyed"}


@dataclass(frozen=True)
class Problem:
    severity: str  # error | warning
    code: str
    target: str  # "scenarios:<key>" / "items:<key>" / a map record ID
    field: str | None
    message: str

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass
class ScenarioContext:
    """Facts a scenario is checked against; built by ``context_for`` from an open map, or by tests."""

    records: dict = field(default_factory=dict)  # record ID -> record (enabled and disabled)
    items: dict = field(default_factory=dict)  # item key -> item definition
    scenarios: set = field(default_factory=set)
    maps: set = field(default_factory=set)
    map_open: bool = True  # False: record references cannot be checked (the map is closed)
    navigation: object = None  # navgraph.NavGraph or None
    teams: int = 4

    def category(self, record_id) -> str | None:
        record = self.records.get(record_id)
        return record.get("category") if record else None

    def placed_items(self, item_key: str) -> tuple[int, bool]:
        """(units available, unlimited) of an item on this map's pickups."""

        total, unlimited = 0, False
        for record in self.records.values():
            if record.get("category") != "item_pickup" or record.get("disabled"):
                continue
            fields = record.get("fields", {})
            if (fields.get("item") or {}).get("value") != item_key:
                continue
            quantity = int((fields.get("quantity") or {}).get("value") or 0)
            uses = int((fields.get("uses") or {}).get("value") or 0)
            respawn = int((fields.get("respawn_seconds") or {}).get("value") or 0)
            if uses == 0 and respawn > 0:
                unlimited = True
            total += quantity * max(uses, 1)
        return total, unlimited


def context_for(project, key: str | None, document, library=None, runtime_profile=None) -> ScenarioContext:
    """Context for scenarios on the map that ``document`` shows (``document`` None: references unchecked)."""

    from .navgraph import NavGraph

    ctx = ScenarioContext(items=dict(project.items), scenarios=set(project.scenarios), maps=set(project.maps), map_open=document is not None,
                          teams=int((runtime_profile or {}).get("teams", 4)))
    if document is not None:
        ctx.records = {record["id"]: record for _, record in document.scene.records()}
        ctx.navigation = NavGraph.from_scene(document.scene)
    return ctx


# ------------------------------------------------------------------- creation --

def new_scenario(title: str, map_key: str, mode: str = "mission") -> dict:
    return {
        "schema": SCHEMA, "version": VERSION, "title": title, "map": map_key,
        "rules": {"mode": mode, "teams": 2, "time_limit_seconds": 0, "score_limit": 0, "lives": 3, "friendly_fire": False,
                  "score": {"kill": 100, "objective": 500, "multipliers": []}},
        "start": {"player_spawn": None, "transition": "briefing", "briefing": "", "actions": []},
        "end": {"victory": {"transition": "results", "next": None, "message": "Mission complete"},
                "failure": {"transition": "retry", "message": "Mission failed"}},
        "victory": [{"when": "all_required_objectives"}], "failure": [{"when": "player_destroyed"}],
        "objectives": {}, "triggers": {}, "timers": {}, "counters": {}, "waves": {}, "groups": {}, "layout": {},
    }


def next_id(prefix: str, existing) -> str:
    """Smallest free ``<prefix><n>`` (deterministic: the same edits always yield the same IDs)."""

    number = 1
    while f"{prefix}{number}" in existing:
        number += 1
    return f"{prefix}{number}"


def new_objective(kind: str, title: str | None = None) -> dict:
    if kind not in OBJECTIVE_KINDS:
        raise KeyError(f"unknown objective kind {kind!r}")
    return {"kind": kind, "title": title or OBJECTIVE_KINDS[kind]["label"], "params": copy.deepcopy(OBJECTIVE_DEFAULTS[kind]),
            "requires": [], "optional": False, "repeat": 1, "hidden": False, "on_complete": [], "on_fail": []}


def new_trigger(title: str = "Trigger") -> dict:
    return {"title": title, "on": "enter", "volume": None, "subject": "player", "once": True, "enabled": True, "actions": []}


def new_timer(title: str = "Timer") -> dict:
    return {"title": title, "seconds": 60, "starts": "at_start", "repeat": False, "visible": True, "on_expire": []}


def new_counter(title: str = "Counter") -> dict:
    return {"title": title, "initial": 0, "min": 0, "max": 100, "visible": True}


def new_wave(title: str = "Wave") -> dict:
    return {"title": title, "bots": [], "spawner": None, "count": 4, "interval_seconds": 5, "delay_seconds": 0, "repeat": 1}


def new_group(title: str = "Group") -> dict:
    return {"title": title, "members": [], "active_at_start": True}


ELEMENT_FACTORIES = {"objectives": ("obj", new_objective), "triggers": ("trg", new_trigger), "timers": ("tmr", new_timer),
                     "counters": ("cnt", new_counter), "waves": ("wav", new_wave), "groups": ("grp", new_group)}
FAMILIES = tuple(ELEMENT_FACTORIES)


def add_element(scenario: dict, family: str, *args) -> str:
    prefix, factory = ELEMENT_FACTORIES[family]
    table = scenario.setdefault(family, {})
    if len(table) >= MAX_ELEMENTS:
        raise ValueError(f"a scenario holds at most {MAX_ELEMENTS} {family}")
    element_id = next_id(prefix, set(table))
    table[element_id] = factory(*args)
    return element_id


def remove_element(scenario: dict, family: str, element_id: str) -> None:
    """Remove an element and every reference to it (requires lists, layout); actions naming it stay and are reported."""

    scenario.get(family, {}).pop(element_id, None)
    scenario.get("layout", {}).pop(element_id, None)
    if family == "objectives":
        for objective in scenario.get("objectives", {}).values():
            objective["requires"] = [r for r in objective.get("requires", []) if r != element_id]


# ------------------------------------------------------------------- analysis --

def _ids(scenario: dict, family: str) -> set:
    table = scenario.get(family)
    return set(table) if isinstance(table, dict) else set()


def action_context(scenario: dict, ctx: ScenarioContext, context: str = "scenario") -> act.ActionContext:
    return act.ActionContext(context, _ids(scenario, "objectives"), _ids(scenario, "triggers"), _ids(scenario, "timers"),
                             _ids(scenario, "counters"), _ids(scenario, "waves"), _ids(scenario, "groups"),
                             {k: v.get("category") for k, v in ctx.records.items()}, set(ctx.items), ctx.map_open)


def requirement_cycle(objectives: dict) -> list[str] | None:
    """One cycle in the ``requires`` graph (as a list of IDs, first repeated last), or None."""

    state: dict[str, int] = {}
    stack: list[str] = []

    def visit(node: str) -> list[str] | None:
        state[node] = 1
        stack.append(node)
        for other in sorted(objectives.get(node, {}).get("requires", []) or []):
            if other not in objectives:
                continue
            if state.get(other) == 1:
                return stack[stack.index(other):] + [other]
            if state.get(other) is None:
                found = visit(other)
                if found:
                    return found
        stack.pop()
        state[node] = 2
        return None

    for node in sorted(objectives):
        if state.get(node) is None:
            found = visit(node)
            if found:
                return found
    return None


def trigger_cycle(triggers: dict) -> list[str] | None:
    """A cycle of triggers that fire each other immediately (``activate_trigger``): an endless loop in one frame."""

    edges = {key: sorted({a.get("trigger") for a in (t.get("actions") or []) if isinstance(a, dict) and a.get("do") == "activate_trigger"
                          and a.get("trigger") in triggers}) for key, t in triggers.items() if isinstance(t, dict)}
    return requirement_cycle({key: {"requires": targets} for key, targets in edges.items()})


def depth(objectives: dict) -> dict[str, int]:
    """Column of each objective in the graph editor: 0 for no requirements, else 1 + deepest requirement (cycles cut)."""

    out: dict[str, int] = {}

    def level(node: str, trail: frozenset) -> int:
        if node in out:
            return out[node]
        requires = [r for r in objectives.get(node, {}).get("requires", []) or [] if r in objectives and r not in trail]
        value = 0 if not requires else 1 + max(level(r, trail | {node}) for r in requires)
        out[node] = value
        return value

    for node in sorted(objectives):
        level(node, frozenset())
    return out


def required_features(scenario: dict) -> list[str]:
    """Runtime features (``core.runtime.FEATURES``) a loader must declare to play this scenario."""

    features = {"scenario", f"scenario.mode.{(scenario.get('rules') or {}).get('mode', 'custom')}"}
    start = scenario.get("start") or {}
    end = scenario.get("end") or {}
    features.add(f"transition.start.{start.get('transition', 'cut')}")
    features.add(f"transition.victory.{(end.get('victory') or {}).get('transition', 'results')}")
    features.add(f"transition.failure.{(end.get('failure') or {}).get('transition', 'retry')}")
    for condition in (scenario.get("victory") or []) + (scenario.get("failure") or []):
        if isinstance(condition, dict):
            features.add(f"condition.{condition.get('when')}")
    actions = list(start.get("actions") or [])
    for objective in (scenario.get("objectives") or {}).values():
        features.add(f"objective.{objective.get('kind')}")
        if objective.get("requires"):
            features.add("objective.flow.requires")
        if objective.get("optional"):
            features.add("objective.flow.optional")
        if objective.get("repeat", 1) != 1:
            features.add("objective.flow.repeat")
        actions += list(objective.get("on_complete") or []) + list(objective.get("on_fail") or [])
    for trigger in (scenario.get("triggers") or {}).values():
        features.add(f"trigger.{trigger.get('on')}")
        actions += list(trigger.get("actions") or [])
    for family in ("timers", "counters", "waves", "groups"):
        if scenario.get(family):
            features.add(f"scenario.{family}")
    for timer in (scenario.get("timers") or {}).values():
        actions += list(timer.get("on_expire") or [])
    if ((scenario.get("rules") or {}).get("score") or {}).get("multipliers"):
        features.add("scenario.score_multipliers")
    for action in actions:
        if isinstance(action, dict) and action.get("do") in act.ACTIONS:
            features.add(act.feature(action["do"]))
    return sorted(features)


# ------------------------------------------------------------------ validation --

def _int(value, lo, hi) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and lo <= value <= hi


def _text(value, limit=120) -> bool:
    return isinstance(value, str) and len(value) <= limit and not any(ord(c) < 32 for c in value)


def check(key: str, scenario, ctx: ScenarioContext) -> list[Problem]:
    target = f"scenarios:{key}"
    out: list[Problem] = []

    def err(code, where, message, severity="error"):
        out.append(Problem(severity, code, target, where, message))

    if not isinstance(scenario, dict):
        return [Problem("error", "INVALID_SCENARIO", target, None, "a scenario must be an object")]
    if scenario.get("schema", SCHEMA) != SCHEMA or not _int(scenario.get("version", VERSION), 1, VERSION):
        err("FUTURE_SCENARIO", "version", f"scenario version {scenario.get('version')} is newer than this Studio supports ({VERSION})")
        return out
    if not _text(scenario.get("title"), 80) or not scenario.get("title"):
        err("INVALID_FIELD", "title", "give the scenario a name (at most 80 characters)")
    if scenario.get("map") not in ctx.maps:
        err("MISSING_MAP", "map", f"map {scenario.get('map')!r} is not in this project")
    rules = scenario.get("rules") if isinstance(scenario.get("rules"), dict) else {}
    if rules.get("mode") not in MODES:
        err("INVALID_FIELD", "rules.mode", f"match mode must be one of {', '.join(MODES)}")
    if not _int(rules.get("teams", 2), 1, ctx.teams):
        err("INVALID_TEAM", "rules.teams", f"teams must be 1-{ctx.teams}")
    for name, hi in (("time_limit_seconds", 7200), ("score_limit", 1_000_000), ("lives", 99)):
        if not _int(rules.get(name, 0), 0, hi):
            err("INVALID_FIELD", f"rules.{name}", f"{name.replace('_', ' ')} must be 0-{hi} (0 = none)")
    score = rules.get("score") if isinstance(rules.get("score"), dict) else {}
    for name in ("kill", "objective"):
        if not _int(score.get(name, 0), -10000, 10000):
            err("INVALID_FIELD", f"rules.score.{name}", "points must be -10000 to 10000")
    multipliers = score.get("multipliers") or []
    for number, multiplier in enumerate(multipliers if isinstance(multipliers, list) else []):
        where = f"rules.score.multipliers[{number}]"
        if not isinstance(multiplier, dict):
            err("INVALID_FIELD", where, "a multiplier must be an object")
            continue
        if multiplier.get("applies") not in ("kills", "objectives", "all"):
            err("INVALID_FIELD", f"{where}.applies", "applies must be kills, objectives or all")
        factor = multiplier.get("factor")
        if not isinstance(factor, (int, float)) or isinstance(factor, bool) or not 0.1 <= factor <= 10:
            err("INVALID_FIELD", f"{where}.factor", "factor must be 0.1-10")
        zone = multiplier.get("zone")
        if zone is not None and ctx.map_open and ctx.category(zone) not in VOLUME_CATEGORIES:
            err("DANGLING_REFERENCE", f"{where}.zone", f"{zone} is not a trigger volume or zone on this map")
        if multiplier.get("while_timer") is not None and multiplier["while_timer"] not in _ids(scenario, "timers"):
            err("DANGLING_REFERENCE", f"{where}.while_timer", f"timer {multiplier['while_timer']!r} does not exist")
    for family in FAMILIES + ("layout",):
        value = scenario.get(family, {})
        if not isinstance(value, dict):
            err("INVALID_FIELD", family, f"{family} must be an object keyed by ID")
        elif len(value) > MAX_ELEMENTS:
            err("LIMIT", family, f"at most {MAX_ELEMENTS} {family}")
        elif family != "layout":
            for element_id in value:
                if not ELEMENT_ID.match(str(element_id)):
                    err("INVALID_ID", f"{family}.{element_id}", "element IDs are 1-32 lowercase letters, digits or underscores")
    if out and any(p.code in ("INVALID_FIELD",) and p.field in FAMILIES for p in out):
        return out
    actx = action_context(scenario, ctx)

    def actions(where, value):
        for field_name, message in act.check_actions(value, actx, where):
            code = "DANGLING_REFERENCE" if "does not exist" in message or "neither" in message else (
                "UNSUPPORTED_ACTION" if "cannot be used" in message or "unknown action" in message else "INVALID_FIELD")
            err(code, field_name, message)

    def record(where, value, categories, required=True):
        if value is None:
            if required:
                err("MISSING_REFERENCE", where, f"choose a {' or '.join(c.replace('_', ' ') for c in categories)} on the map")
            return False
        if not ctx.map_open:
            return True
        found = ctx.records.get(value)
        if found is None:
            err("DANGLING_REFERENCE", where, f"{value} is not on map {scenario.get('map')!r}")
            return False
        if found.get("category") not in categories:
            err("INVALID_REFERENCE", where, f"{value} is a {found.get('category')}, not a {' or '.join(categories)}")
            return False
        if found.get("disabled"):
            err("DISABLED_REFERENCE", where, f"{value} is disabled on the map")
            return False
        return True

    # start and end
    start = scenario.get("start") if isinstance(scenario.get("start"), dict) else {}
    if start.get("transition") not in TRANSITIONS_START:
        err("INVALID_FIELD", "start.transition", f"start transition must be one of {', '.join(TRANSITIONS_START)}")
    if not _text(start.get("briefing", ""), 600):
        err("INVALID_FIELD", "start.briefing", "briefing must be at most 600 printable characters")
    if start.get("player_spawn") is not None or rules.get("mode") == "mission":
        record("start.player_spawn", start.get("player_spawn"), SPAWN_CATEGORIES)
    actions("start.actions", start.get("actions"))
    end = scenario.get("end") if isinstance(scenario.get("end"), dict) else {}
    victory_end = end.get("victory") if isinstance(end.get("victory"), dict) else {}
    failure_end = end.get("failure") if isinstance(end.get("failure"), dict) else {}
    if victory_end.get("transition") not in TRANSITIONS_VICTORY:
        err("INVALID_FIELD", "end.victory.transition", f"victory transition must be one of {', '.join(TRANSITIONS_VICTORY)}")
    if failure_end.get("transition") not in TRANSITIONS_FAILURE:
        err("INVALID_FIELD", "end.failure.transition", f"failure transition must be one of {', '.join(TRANSITIONS_FAILURE)}")
    if victory_end.get("transition") == "next_scenario":
        following = victory_end.get("next")
        if following not in ctx.scenarios:
            err("DANGLING_REFERENCE", "end.victory.next", "choose the scenario that follows a victory")
        elif following == key:
            err("SCENARIO_CYCLE", "end.victory.next", "a scenario cannot lead to itself")
    for name, value in (("end.victory.message", victory_end.get("message", "")), ("end.failure.message", failure_end.get("message", ""))):
        if not _text(value):
            err("INVALID_FIELD", name, "messages must be at most 120 printable characters")

    objectives = scenario.get("objectives") or {}
    triggers = scenario.get("triggers") or {}
    timers = scenario.get("timers") or {}
    counters = scenario.get("counters") or {}
    impossible: set[str] = set()

    # victory and failure conditions
    for family_name in ("victory", "failure"):
        conditions = scenario.get(family_name)
        if not isinstance(conditions, list):
            err("INVALID_FIELD", family_name, f"{family_name} must be a list of conditions")
            continue
        if family_name == "victory" and not conditions and rules.get("mode") in ("mission", "survival", "race", "custom"):
            err("NO_VICTORY", "victory", "add at least one victory condition; otherwise the scenario can never be won")
        for number, condition in enumerate(conditions):
            where = f"{family_name}[{number}]"
            when = condition.get("when") if isinstance(condition, dict) else None
            if when not in CONDITIONS:
                err("INVALID_FIELD", where, f"unknown condition {when!r}")
                continue
            if family_name == "victory" and when in FAILURE_ONLY or family_name == "failure" and when in VICTORY_ONLY:
                err("INVALID_FIELD", where, f"'{when.replace('_', ' ')}' cannot be a {family_name} condition")
            for param, kind in CONDITIONS[when].items():
                value = condition.get(param)
                if kind == "objective" and value not in objectives:
                    err("DANGLING_REFERENCE", f"{where}.{param}", f"objective {value!r} does not exist")
                elif kind == "timer" and value not in timers:
                    err("DANGLING_REFERENCE", f"{where}.{param}", f"timer {value!r} does not exist")
                elif kind == "counter" and value not in counters:
                    err("DANGLING_REFERENCE", f"{where}.{param}", f"counter {value!r} does not exist")
                elif kind == "record":
                    record(f"{where}.{param}", value, DESTRUCTIBLE_CATEGORIES)
                elif kind == "int" and not _int(value, -1_000_000, 1_000_000):
                    err("INVALID_FIELD", f"{where}.{param}", "value must be a whole number")
            if when == "score_reached" and _int(condition.get("value"), 1, 10**9) and rules.get("score_limit") and \
                    condition["value"] > rules["score_limit"]:
                err("IMPOSSIBLE_CONDITION", where, f"score {condition['value']} is above the match score limit {rules['score_limit']}")
            if when == "all_required_objectives" and not any(not o.get("optional") for o in objectives.values() if isinstance(o, dict)):
                err("IMPOSSIBLE_CONDITION", where, "'all required objectives' needs at least one objective that is not optional")

    # objectives
    for element_id, objective in sorted(objectives.items()):
        where = f"objectives.{element_id}"
        if not isinstance(objective, dict):
            err("INVALID_FIELD", where, "an objective must be an object")
            continue
        kind = objective.get("kind")
        spec = OBJECTIVE_KINDS.get(kind)
        if spec is None:
            err("UNSUPPORTED_OBJECTIVE", f"{where}.kind", f"unknown objective kind {kind!r}")
            continue
        if not _text(objective.get("title"), 80) or not objective.get("title"):
            err("INVALID_FIELD", f"{where}.title", "give the objective a name")
        for required in objective.get("requires") or []:
            if required not in objectives:
                err("DANGLING_REFERENCE", f"{where}.requires", f"required objective {required!r} does not exist")
            elif (objectives[required] or {}).get("repeat", 1) == 0:
                err("IMPOSSIBLE_OBJECTIVE", f"{where}.requires", f"{required} repeats for the whole match and never finishes, so it cannot be required")
                impossible.add(element_id)
        if not _int(objective.get("repeat", 1), 0, 99):
            err("INVALID_FIELD", f"{where}.repeat", "repeat must be 0 (whole match) to 99")
        params = objective.get("params") if isinstance(objective.get("params"), dict) else {}
        for param, kind_spec in spec["params"].items():
            value = params.get(param)
            place = f"{where}.params.{param}"
            if kind_spec[0] == "int":
                if not _int(value, kind_spec[1], kind_spec[2]):
                    err("INVALID_FIELD", place, f"{param.replace('_', ' ')} must be {kind_spec[1]}-{kind_spec[2]}")
            elif kind_spec[0] == "record":
                if not record(place, value, kind_spec[1]):
                    impossible.add(element_id)
            elif kind_spec[0] == "records":
                if not isinstance(value, list) or not value:
                    err("MISSING_REFERENCE", place, f"choose at least one {' or '.join(c.replace('_', ' ') for c in kind_spec[1][:3])}")
                    impossible.add(element_id)
                else:
                    if len(set(value)) != len(value):
                        err("INVALID_FIELD", place, "the same object is listed twice")
                    for item in value:
                        if not record(place, item, kind_spec[1]):
                            impossible.add(element_id)
            elif kind_spec[0] == "item":
                if value not in ctx.items:
                    err("DANGLING_REFERENCE", place, f"item {value!r} is not defined in this project")
                    impossible.add(element_id)
            elif kind_spec[0] == "counter":
                if value not in counters:
                    err("DANGLING_REFERENCE", place, f"counter {value!r} does not exist")
                    impossible.add(element_id)
            elif kind_spec[0] == "trigger":
                if value not in triggers:
                    err("DANGLING_REFERENCE", place, f"trigger {value!r} does not exist")
                    impossible.add(element_id)
            elif kind_spec[0] == "team":
                if not _int(value, 0, max(0, int(rules.get("teams", 2) or 1) - 1)):
                    err("INVALID_TEAM", place, f"team must be 0-{max(0, int(rules.get('teams', 2) or 1) - 1)} (the match has {rules.get('teams', 2)} teams)")
            elif kind_spec[0] == "team_or_any":
                if not _int(value, -1, max(0, int(rules.get("teams", 2) or 1) - 1)):
                    err("INVALID_TEAM", place, "team must be -1 (any) or a team of the match")
        impossible |= _impossible(element_id, objective, params, scenario, rules, ctx, err)
        actions(f"{where}.on_complete", objective.get("on_complete"))
        actions(f"{where}.on_fail", objective.get("on_fail"))
    cycle = requirement_cycle({k: v for k, v in objectives.items() if isinstance(v, dict)})
    if cycle:
        err("OBJECTIVE_CYCLE", f"objectives.{cycle[0]}.requires", "objectives require each other in a loop: " + " -> ".join(cycle))
    # an objective that needs an impossible one is impossible too
    changed = True
    while changed:
        changed = False
        for element_id, objective in objectives.items():
            if element_id not in impossible and isinstance(objective, dict) and any(r in impossible for r in objective.get("requires") or []):
                impossible.add(element_id)
                err("IMPOSSIBLE_OBJECTIVE", f"objectives.{element_id}.requires", f"it needs an objective that can never be completed")
                changed = True
    if objectives and all(element_id in impossible or (o or {}).get("optional") for element_id, o in objectives.items()) and \
            any(isinstance(c, dict) and c.get("when") == "all_required_objectives" for c in scenario.get("victory") or []) and \
            any(element_id in impossible for element_id in objectives):
        err("IMPOSSIBLE_VICTORY", "victory", "every required objective is impossible, so the scenario can never be won")

    # triggers
    for element_id, trigger in sorted(triggers.items()):
        where = f"triggers.{element_id}"
        if not isinstance(trigger, dict):
            err("INVALID_FIELD", where, "a trigger must be an object")
            continue
        event = trigger.get("on")
        if event not in TRIGGER_EVENTS:
            err("UNSUPPORTED_TRIGGER", f"{where}.on", f"unknown trigger event {event!r}")
            continue
        if event in VOLUME_EVENTS:
            record(f"{where}.volume", trigger.get("volume"), VOLUME_CATEGORIES)
            if trigger.get("subject") not in SUBJECTS:
                err("INVALID_FIELD", f"{where}.subject", f"subject must be one of {', '.join(SUBJECTS)}")
            if event == "inside" and not _int(trigger.get("seconds"), 1, 3600):
                err("INVALID_FIELD", f"{where}.seconds", "seconds inside must be 1-3600")
        elif event in ("objective_completed", "objective_failed") and trigger.get("objective") not in objectives:
            err("DANGLING_REFERENCE", f"{where}.objective", f"objective {trigger.get('objective')!r} does not exist")
        elif event == "timer_expired" and trigger.get("timer") not in timers:
            err("DANGLING_REFERENCE", f"{where}.timer", f"timer {trigger.get('timer')!r} does not exist")
        elif event == "counter_reached":
            if trigger.get("counter") not in counters:
                err("DANGLING_REFERENCE", f"{where}.counter", f"counter {trigger.get('counter')!r} does not exist")
            else:
                counter = counters[trigger["counter"]]
                value = trigger.get("value")
                if not _int(value, -1_000_000, 1_000_000) or isinstance(counter, dict) and not counter.get("min", 0) <= value <= counter.get("max", 0):
                    err("IMPOSSIBLE_TRIGGER", f"{where}.value", "the value lies outside the counter's range, so the trigger never fires")
        elif event == "destroyed":
            record(f"{where}.record", trigger.get("record"), DESTRUCTIBLE_CATEGORIES)
        if not isinstance(trigger.get("once", True), bool) or not isinstance(trigger.get("enabled", True), bool):
            err("INVALID_FIELD", where, "once and enabled are yes/no choices")
        actions(f"{where}.actions", trigger.get("actions"))
    cycle = trigger_cycle({k: v for k, v in triggers.items() if isinstance(v, dict)})
    if cycle:
        err("TRIGGER_CYCLE", f"triggers.{cycle[0]}.actions", "triggers fire each other in a loop: " + " -> ".join(cycle))

    # timers, counters, waves, groups
    started = {a.get("timer") for a in _all_actions(scenario) if a.get("do") == "start_timer"}
    for element_id, timer in sorted(timers.items()):
        where = f"timers.{element_id}"
        if not isinstance(timer, dict) or not _int(timer.get("seconds"), 1, 7200):
            err("INVALID_FIELD", f"{where}.seconds", "a timer runs 1-7200 seconds")
            continue
        if timer.get("starts") not in ("at_start", "by_action"):
            err("INVALID_FIELD", f"{where}.starts", "a timer starts at the start or when an action starts it")
        elif timer["starts"] == "by_action" and element_id not in started:
            err("TIMER_NEVER_STARTS", f"{where}.starts", "no action starts this timer", "warning")
        actions(f"{where}.on_expire", timer.get("on_expire"))
    for element_id, counter in sorted(counters.items()):
        where = f"counters.{element_id}"
        if not isinstance(counter, dict) or not all(_int(counter.get(n), -1_000_000, 1_000_000) for n in ("initial", "min", "max")):
            err("INVALID_FIELD", where, "initial, min and max must be whole numbers")
        elif not counter["min"] <= counter["initial"] <= counter["max"]:
            err("INVALID_FIELD", f"{where}.initial", "the starting value must lie between min and max")
    for element_id, wave in sorted((scenario.get("waves") or {}).items()):
        where = f"waves.{element_id}"
        if not isinstance(wave, dict):
            err("INVALID_FIELD", where, "a wave must be an object")
            continue
        bots = wave.get("bots") or []
        if not bots and wave.get("spawner") is None:
            err("MISSING_REFERENCE", where, "choose the bots or the unit generator this wave sends")
        for bot in bots:
            record(f"{where}.bots", bot, ("bot_slot",))
        if wave.get("spawner") is not None:
            record(f"{where}.spawner", wave.get("spawner"), ("unit_spawner",))
        for name, lo, hi in (("count", 1, 64), ("interval_seconds", 0, 600), ("delay_seconds", 0, 3600), ("repeat", 0, 99)):
            if not _int(wave.get(name), lo, hi):
                err("INVALID_FIELD", f"{where}.{name}", f"{name.replace('_', ' ')} must be {lo}-{hi}")
    for element_id, group in sorted((scenario.get("groups") or {}).items()):
        where = f"groups.{element_id}"
        members = group.get("members") if isinstance(group, dict) else None
        if not isinstance(members, list) or not members:
            err("EMPTY_GROUP", f"{where}.members", "add at least one object to the group")
            continue
        for member in members:
            if ctx.map_open and member not in ctx.records:
                err("DANGLING_REFERENCE", f"{where}.members", f"{member} is not on this map")
    return out


def _all_actions(scenario: dict) -> list[dict]:
    out = list((scenario.get("start") or {}).get("actions") or [])
    for objective in (scenario.get("objectives") or {}).values():
        if isinstance(objective, dict):
            out += list(objective.get("on_complete") or []) + list(objective.get("on_fail") or [])
    for trigger in (scenario.get("triggers") or {}).values():
        if isinstance(trigger, dict):
            out += list(trigger.get("actions") or [])
    for timer in (scenario.get("timers") or {}).values():
        if isinstance(timer, dict):
            out += list(timer.get("on_expire") or [])
    return [a for a in out if isinstance(a, dict)]


def _impossible(element_id, objective, params, scenario, rules, ctx, err) -> set[str]:
    """Kind-specific reasons an objective can never be completed."""

    out = set()
    where = f"objectives.{element_id}.params"
    kind = objective.get("kind")
    limit = rules.get("time_limit_seconds") or 0
    if kind == "destroy" and isinstance(params.get("targets"), list) and _int(params.get("count"), 1, 999) \
            and params["count"] > len(params["targets"]):
        err("IMPOSSIBLE_OBJECTIVE", f"{where}.count", f"asks for {params['count']} of only {len(params['targets'])} target(s)")
        out.add(element_id)
    if kind in ("survive", "defend") and limit and _int(params.get("seconds"), 1, 7200) and params["seconds"] > limit:
        err("IMPOSSIBLE_OBJECTIVE", f"{where}.seconds", f"lasts {params['seconds']} s but the match ends after {limit} s")
        out.add(element_id)
    if kind == "capture" and limit and _int(params.get("hold_seconds"), 1, 3600) and params["hold_seconds"] > limit:
        err("IMPOSSIBLE_OBJECTIVE", f"{where}.hold_seconds", f"holding takes {params['hold_seconds']} s but the match ends after {limit} s")
        out.add(element_id)
    if kind == "race" and isinstance(params.get("checkpoints"), list) and 0 < len(params["checkpoints"]) < 2:
        err("IMPOSSIBLE_OBJECTIVE", f"{where}.checkpoints", "a race needs at least two checkpoints")
        out.add(element_id)
    if kind == "collect" and params.get("item") in ctx.items and ctx.map_open and _int(params.get("count"), 1, 999):
        available, unlimited = ctx.placed_items(params["item"])
        if not unlimited and available < params["count"]:
            err("IMPOSSIBLE_OBJECTIVE", f"{where}.count", f"needs {params['count']} but the map's pickups give only {available}")
            out.add(element_id)
    if kind in ("boss", "escort") and ctx.map_open:
        bot = ctx.records.get(params.get("boss" if kind == "boss" else "escortee"))
        if bot is not None and bot.get("category") == "bot_slot":
            role = ((bot.get("fields") or {}).get("role") or {}).get("value")
            if kind == "boss" and role != 6:
                err("INVALID_REFERENCE", f"{where}.boss", "this bot's behaviour role is not 'boss'")
                out.add(element_id)
    if kind == "kill_count" and rules.get("mode") == "mission" and ctx.map_open:
        player_team = 0
        enemies = [r for r in ctx.records.values() if not r.get("disabled") and (
            r.get("category") == "bot_slot" and ((r.get("fields") or {}).get("team") or {}).get("value") != player_team
            or r.get("category") in ("unit_spawner",))]
        if not enemies:
            err("IMPOSSIBLE_OBJECTIVE", f"{where}.count", "there are no enemy bots or unit generators on this map to destroy")
            out.add(element_id)
    if kind in ("reach", "escort", "capture", "defend") and ctx.navigation and ctx.map_open:
        start = ctx.records.get((scenario.get("start") or {}).get("player_spawn"))
        zone = ctx.records.get(params.get({"reach": "area", "escort": "destination", "capture": "zone", "defend": "zone"}[kind]))
        if start is not None and zone is not None:
            a = ctx.navigation.nearest(start["translation"][0], start["translation"][2])
            b = ctx.navigation.nearest(zone["translation"][0], zone["translation"][2])
            if a and b and not ctx.navigation.connected(a, b):
                err("UNREACHABLE_OBJECTIVE", f"{where}", "no navigation path links the player's start to this objective's area")
                out.add(element_id)
    return out


# ------------------------------------------------------------------ migration --

def from_mission(key: str, mission: dict) -> dict:
    """A scenario equivalent to a project-v2 mission: its map, briefing and objective markers (all required, in parallel)."""

    scenario = new_scenario(mission.get("title") or key, mission.get("map"))
    scenario["start"]["briefing"] = mission.get("briefing", "") or ""
    for marker in mission.get("objectives", []) or []:
        element_id = next_id("obj", scenario["objectives"])
        objective = new_objective("marker_objective", "Objective")
        objective["params"]["marker"] = marker
        scenario["objectives"][element_id] = objective
    scenario["migrated_from"] = {"family": "missions", "key": key}
    return scenario
