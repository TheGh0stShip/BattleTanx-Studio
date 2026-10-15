"""The constrained, declarative action vocabulary shared by scenarios and items.

Scenario triggers, timers and objectives, and item effects, never carry code.
They carry a list of actions, each a small typed record::

    {"do": "modify_stat", "stat": "health", "amount": 50, "mode": "add"}

``ACTIONS`` lists every action kind with its parameters, the contexts it may
appear in ("scenario", "item") and the runtime feature a loader must declare
to execute it.  ``check_action`` validates one action against a context that
knows which objectives, triggers, timers, counters, waves, groups, map
records and items exist; it returns ``(field, message)`` pairs and never
raises.  Unknown keys in an action are preserved by every caller and ignored
here, so a newer Studio's extra parameters survive a round trip.

Parameter types:
    ("int", lo, hi)        integer in range
    ("float", lo, hi)      number in range
    ("enum", values)       one of the listed strings
    ("text", max_length)   printable text
    ("ref", family)        an ID that must exist in the context family
                           (objective, trigger, timer, counter, wave, group,
                           record, item, scenario)
    ("sound", None)        a sound reference: "studio:sound:<name>" or a content ID
Parameters whose type tuple ends with ``"optional"`` may be omitted or None.
"""

from __future__ import annotations

from dataclasses import dataclass, field

STATS = ("health", "armor", "ammunition", "currency", "score")
TIMED_STATS = ("speed", "damage", "armor", "fire_rate", "turn_rate")
WEAPONS = ("cannon", "machine_gun", "rockets", "guided_missiles", "mines", "special")
AUDIENCES = ("everyone", "player", "team_0", "team_1", "team_2", "team_3")
MAX_ACTIONS = 32
MAX_TEXT = 120

ACTIONS: dict[str, dict] = {
    # stats, weapons and items (what pickups do)
    "modify_stat": {"params": {"stat": ("enum", STATS), "amount": ("int", -100000, 100000), "mode": ("enum", ("add", "set", "percent"))},
                    "contexts": ("scenario", "item"), "label": "Change health, armor, ammunition, currency or score"},
    "grant_weapon": {"params": {"weapon": ("enum", WEAPONS), "ammunition": ("int", 0, 999)},
                     "contexts": ("scenario", "item"), "label": "Give a weapon"},
    "remove_weapon": {"params": {"weapon": ("enum", WEAPONS)}, "contexts": ("scenario", "item"), "label": "Take a weapon away"},
    "grant_item": {"params": {"item": ("ref", "item"), "count": ("int", 1, 99)}, "contexts": ("scenario", "item"), "label": "Give an item"},
    "remove_item": {"params": {"item": ("ref", "item"), "count": ("int", 1, 99)}, "contexts": ("scenario", "item"), "label": "Take an item away"},
    "timed_modifier": {"params": {"stat": ("enum", TIMED_STATS), "factor": ("float", 0.1, 10.0), "seconds": ("int", 1, 600)},
                       "contexts": ("scenario", "item"), "label": "Change a stat for a while"},
    # scenario state
    "counter_add": {"params": {"counter": ("ref", "counter"), "amount": ("int", -10000, 10000)}, "contexts": ("scenario", "item"),
                    "label": "Add to a counter"},
    "counter_set": {"params": {"counter": ("ref", "counter"), "value": ("int", -100000, 100000)}, "contexts": ("scenario",),
                    "label": "Set a counter"},
    "start_timer": {"params": {"timer": ("ref", "timer")}, "contexts": ("scenario",), "label": "Start a timer"},
    "stop_timer": {"params": {"timer": ("ref", "timer")}, "contexts": ("scenario",), "label": "Stop a timer"},
    "enable_trigger": {"params": {"trigger": ("ref", "trigger")}, "contexts": ("scenario", "item"), "label": "Switch a trigger on"},
    "disable_trigger": {"params": {"trigger": ("ref", "trigger")}, "contexts": ("scenario",), "label": "Switch a trigger off"},
    "activate_trigger": {"params": {"trigger": ("ref", "trigger")}, "contexts": ("scenario", "item"), "label": "Fire a trigger now"},
    "complete_objective": {"params": {"objective": ("ref", "objective")}, "contexts": ("scenario", "item"), "label": "Complete an objective"},
    "fail_objective": {"params": {"objective": ("ref", "objective")}, "contexts": ("scenario",), "label": "Fail an objective"},
    "activate": {"params": {"target": ("ref", "record_or_group")}, "contexts": ("scenario",), "label": "Switch an object or group on"},
    "deactivate": {"params": {"target": ("ref", "record_or_group")}, "contexts": ("scenario",), "label": "Switch an object or group off"},
    "spawn_wave": {"params": {"wave": ("ref", "wave")}, "contexts": ("scenario",), "label": "Send a wave"},
    "score_multiplier": {"params": {"factor": ("float", 0.1, 10.0), "seconds": ("int", 0, 3600)}, "contexts": ("scenario", "item"),
                         "label": "Multiply score (0 s = until changed)"},
    "end_scenario": {"params": {"result": ("enum", ("victory", "failure"))}, "contexts": ("scenario",), "label": "End the scenario"},
    # presentation
    "message": {"params": {"text": ("text", MAX_TEXT), "seconds": ("int", 1, 30), "audience": ("enum", AUDIENCES)},
                "contexts": ("scenario", "item"), "label": "Show a message"},
    "play_sound": {"params": {"sound": ("sound", None)}, "contexts": ("scenario", "item"), "label": "Play a sound"},
}

DEFAULTS = {
    "modify_stat": {"stat": "health", "amount": 25, "mode": "add"},
    "grant_weapon": {"weapon": "rockets", "ammunition": 10},
    "remove_weapon": {"weapon": "rockets"},
    "grant_item": {"item": None, "count": 1},
    "remove_item": {"item": None, "count": 1},
    "timed_modifier": {"stat": "speed", "factor": 1.5, "seconds": 15},
    "counter_add": {"counter": None, "amount": 1},
    "counter_set": {"counter": None, "value": 0},
    "start_timer": {"timer": None},
    "stop_timer": {"timer": None},
    "enable_trigger": {"trigger": None},
    "disable_trigger": {"trigger": None},
    "activate_trigger": {"trigger": None},
    "complete_objective": {"objective": None},
    "fail_objective": {"objective": None},
    "activate": {"target": None},
    "deactivate": {"target": None},
    "spawn_wave": {"wave": None},
    "score_multiplier": {"factor": 2.0, "seconds": 30},
    "end_scenario": {"result": "victory"},
    "message": {"text": "Message", "seconds": 4, "audience": "everyone"},
    "play_sound": {"sound": "studio:sound:alert"},
}
STUDIO_SOUNDS = ("alert", "pickup", "objective_complete", "objective_failed", "countdown", "victory", "defeat")


def feature(kind: str) -> str:
    """Runtime feature identifier a loader must declare to execute this action kind."""

    return f"action.{kind}"


def new_action(kind: str) -> dict:
    if kind not in ACTIONS:
        raise KeyError(f"unknown action {kind!r}")
    return {"do": kind, **DEFAULTS[kind]}


@dataclass
class ActionContext:
    """What an action may refer to.  ``records`` maps a map record ID to its category."""

    context: str = "scenario"  # scenario | item
    objectives: set = field(default_factory=set)
    triggers: set = field(default_factory=set)
    timers: set = field(default_factory=set)
    counters: set = field(default_factory=set)
    waves: set = field(default_factory=set)
    groups: set = field(default_factory=set)
    records: dict = field(default_factory=dict)
    items: set = field(default_factory=set)
    check_refs: bool = True  # items are checked again per map, where trigger and objective names are known


def _printable(value) -> bool:
    return isinstance(value, str) and not any(ord(c) < 32 for c in value)


def check_action(action, ctx: ActionContext) -> list[tuple[str, str]]:
    """``[(field, message)]`` for one action; empty when it is valid in ``ctx``."""

    if not isinstance(action, dict):
        return [("do", "an action must be an object")]
    kind = action.get("do")
    spec = ACTIONS.get(kind)
    if spec is None:
        return [("do", f"unknown action {kind!r}; choose one from the list")]
    if ctx.context not in spec["contexts"]:
        return [("do", f"'{spec['label']}' cannot be used in {'an item effect' if ctx.context == 'item' else 'a scenario'}")]
    out = []
    for name, kind_spec in spec["params"].items():
        value = action.get(name)
        kind_name = kind_spec[0]
        if kind_name == "int":
            if not isinstance(value, int) or isinstance(value, bool) or not kind_spec[1] <= value <= kind_spec[2]:
                out.append((name, f"{name} must be a whole number from {kind_spec[1]} to {kind_spec[2]}"))
        elif kind_name == "float":
            if not isinstance(value, (int, float)) or isinstance(value, bool) or not kind_spec[1] <= value <= kind_spec[2]:
                out.append((name, f"{name} must be a number from {kind_spec[1]} to {kind_spec[2]}"))
        elif kind_name == "enum":
            if value not in kind_spec[1]:
                out.append((name, f"{name} must be one of {', '.join(kind_spec[1])}"))
        elif kind_name == "text":
            if not _printable(value) or not value or len(value) > kind_spec[1]:
                out.append((name, f"{name} must be 1-{kind_spec[1]} printable characters"))
        elif kind_name == "sound":
            if not isinstance(value, str) or not (value.startswith("studio:sound:") and value.split(":", 2)[2] in STUDIO_SOUNDS
                                                  or value.count(":") >= 2 and not value.startswith("studio:sound:")):
                out.append((name, f"choose a sound ({', '.join(STUDIO_SOUNDS)}) or a sound resource"))
        elif kind_name == "ref":
            out += _check_ref(name, value, kind_spec[1], ctx)
    return out


def _check_ref(name: str, value, family: str, ctx: ActionContext) -> list[tuple[str, str]]:
    if not isinstance(value, str) or not value:
        return [(name, f"choose the {family.replace('_or_', ' or ')} this action uses")]
    if not ctx.check_refs and family in ("trigger", "objective", "counter", "timer", "wave", "record_or_group"):
        return []
    table = {"objective": ctx.objectives, "trigger": ctx.triggers, "timer": ctx.timers, "counter": ctx.counters, "wave": ctx.waves,
             "group": ctx.groups, "item": ctx.items}
    if family == "record_or_group":
        if value in ctx.groups or value in ctx.records:
            return []
        return [(name, f"{value} is neither a group of this scenario nor an object on its map")]
    if value not in table.get(family, set()):
        return [(name, f"{family} {value!r} does not exist")]
    return []


def check_actions(actions, ctx: ActionContext, where: str) -> list[tuple[str, str]]:
    """Check a list of actions; fields are reported as ``<where>[n].<param>``."""

    if actions is None:
        return []
    if not isinstance(actions, list):
        return [(where, "actions must be a list")]
    out = []
    if len(actions) > MAX_ACTIONS:
        out.append((where, f"at most {MAX_ACTIONS} actions"))
    for number, action in enumerate(actions):
        out += [(f"{where}[{number}].{name}", message) for name, message in check_action(action, ctx)]
    return out


def describe(action: dict, names=None) -> str:
    """Plain-language summary for lists and the scenario graph (``names`` maps IDs to display names)."""

    names = names or {}
    kind = action.get("do")
    spec = ACTIONS.get(kind)
    if spec is None:
        return f"unknown action {kind!r}"
    parts = []
    for name in spec["params"]:
        value = action.get(name)
        parts.append(f"{name.replace('_', ' ')} {names.get(value, value)}")
    return f"{spec['label']}: " + ", ".join(parts)


def referenced(action: dict) -> list[tuple[str, str]]:
    """``[(family, id)]`` an action depends on (for dependency and cycle analysis)."""

    spec = ACTIONS.get(action.get("do") if isinstance(action, dict) else None)
    if spec is None:
        return []
    return [(kind_spec[1], action.get(name)) for name, kind_spec in spec["params"].items()
            if kind_spec[0] == "ref" and isinstance(action.get(name), str)]
