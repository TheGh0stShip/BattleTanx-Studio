# Scenarios, items, bots and Queenlords

Studio authors gameplay in a platform-neutral form. Everything described
here is supported by Studio: it can be edited graphically, validated, saved,
reopened and packaged deterministically. None of it is playable until a PC
runtime declares the features it uses (see `RUNTIME_FEATURES.md` and
`RUNTIME_LAUNCH.md`). Studio never describes content as playable on its own.

Reference code: `tools/btga_studio/core/scenario.py`, `items.py`,
`bots.py`, `actions.py`, `navgraph.py`, `authoring.py` and
`game_models.py`. Editors: `tools/btga_studio/gui/authoring.py`.

## Where things live

| What | Stored in | Edited through |
|---|---|---|
| Scenarios | `project.scenarios` (key -> scenario) | project undo history, one transaction per user action |
| Item definitions | `project.items` (key -> item) | project undo history |
| Trigger volumes, zones, destructible targets, bots, item pickups | map records (Studio templates, starter v3) | map undo history (overlay edits) |
| Runtime program and cartridge path | the user's local settings | never in a project or package |

Project files are `btga-studio-project` version 3. Version 2 files migrate on
load: every mission becomes a scenario with the same key (its map, briefing
and objective markers as required parallel objectives) and the mission
names it in `scenario`. Version 1 files migrate through version 2. Keys this
Studio does not know are kept at every level: project, scenario, element and
action.

## Scenario model (`btga-scenario` version 1)

```
{"schema": "btga-scenario", "version": 1, "title", "map",
 "rules": {"mode", "teams", "time_limit_seconds", "score_limit", "lives", "friendly_fire",
           "score": {"kill", "objective", "multipliers": [{"id", "applies", "factor", "zone"?, "while_timer"?}]}},
 "start": {"player_spawn", "transition", "briefing", "actions"},
 "end": {"victory": {"transition", "next"?, "message"}, "failure": {"transition", "message"}},
 "victory": [condition], "failure": [condition],
 "objectives": {id: objective}, "triggers": {id: trigger}, "timers": {id: timer},
 "counters": {id: counter}, "waves": {id: wave}, "groups": {id: group}, "layout": {id: [x, y]}}
```

Element IDs are `obj1`, `trg1`, `tmr1`, `cnt1`, `wav1`, `grp1`... and the
smallest free number is always used, so the same edits give the same IDs.
Map records are referenced by their stable record IDs; items by key.

**Objectives** have a `kind` (destroy, capture, defend, survive, escort,
collect, race, kill_count, boss, reach, counter, trigger, marker_objective)
with typed `params`, plus `requires` (objective IDs that must be complete
first), `optional`, `repeat` (1 once, n times, 0 for the whole match) and
`on_complete` / `on_fail` action lists. Ordered objectives chain through
`requires`; objectives with the same requirements run in parallel.

**Triggers** fire `actions` on an event: entering, leaving or staying in a
trigger volume or zone (with a subject: the player, any tank, a team or any
bot), the scenario starting, an objective completing or failing, a timer
running out, a counter reaching a value, or a record being destroyed. Each
has `once` and `enabled` (at the start).

**Timers** (seconds, start at the start or by an action, repeat, visible,
`on_expire` actions), **counters** (initial, min, max), **waves** (bots or a
unit generator, count, interval, delay, repeat) and **groups** (map records
switched on and off together) complete the model. Teleporter entrance and
exit pairs, destructible targets and capture/defend zones are map records
that scenarios refer to; the scenario editor's Map objects tab pairs
teleporters.

Victory and failure are lists of conditions; any one ends the scenario:
`all_required_objectives`, `objective`, `score_reached`, `last_team_standing`,
`timer_expired`, `counter_reached` (both sides), `player_destroyed`,
`objective_failed`, `record_destroyed` (failure only).

## Actions (`core/actions.py`)

Triggers, timers, objectives and item effects carry declarative actions,
never code: `modify_stat`, `grant_weapon`, `remove_weapon`, `grant_item`,
`remove_item`, `timed_modifier`, `counter_add`, `counter_set`,
`start_timer`, `stop_timer`, `enable_trigger`, `disable_trigger`,
`activate_trigger`, `complete_objective`, `fail_objective`, `activate`,
`deactivate`, `spawn_wave`, `score_multiplier`, `end_scenario`, `message`,
`play_sound`. Each has typed parameters with ranges, the contexts it may
appear in (scenario, item) and a runtime feature `action.<kind>`.

## Items

```
{"title", "category", "model"?, "icon", "effects": [action], "duration_seconds", "stacking", "max_stack"}
```

Categories: health, armor, ammunition, weapon, currency, score, modifier
(temporary), key, scripted. Stacking: none, refresh, stack. Items are
defined once per project and placed on any map with `item_pickup` records
(item, quantity, respawn seconds, uses (0 unlimited), team (255 any)).

## Bots and Queenlords

A `bot_slot` record is a tank or Queenlord placed where it starts, with
`ai_slot`, `kind` (tank, Queenlord), `model`, `team`, `role` (attack,
defend, patrol, capture, escort, support, boss), `aggression`, the target
its role needs (`patrol_start`, `guard_target`, `capture_target`,
`escort_target`), `respawn`, `respawn_seconds`, `spawn_delay_seconds`,
`capture_behavior` and `allowed_weapons` (cannon, machine gun, rockets,
guided missiles, mines, special). Placing a bot claims the next free slot.
A Queenlord can be a boss or an allied defender, escort or support.
How bots behave in play is decided by the runtime.

## Validation

Every problem names its scenario, item or record and the field.

| Code | Meaning |
|---|---|
| `DANGLING_REFERENCE`, `MISSING_REFERENCE`, `INVALID_REFERENCE`, `DISABLED_REFERENCE` | a reference is missing, empty, of the wrong kind or disabled |
| `OBJECTIVE_CYCLE`, `TRIGGER_CYCLE`, `SCENARIO_CYCLE`, `ITEM_CYCLE` | forbidden loops (requirements, triggers firing each other, a scenario leading to itself, items giving each other) |
| `IMPOSSIBLE_OBJECTIVE`, `IMPOSSIBLE_CONDITION`, `IMPOSSIBLE_VICTORY`, `IMPOSSIBLE_TRIGGER`, `NO_VICTORY` | content that can never complete (more targets than exist, longer than the time limit, too few pickups, requiring a whole-match objective, ...) |
| `UNREACHABLE_OBJECTIVE`, `UNREACHABLE_TARGET`, `NO_NAVIGATION`, `BAD_PATROL` | navigation does not connect the start or a bot to its target, or a patrol goes nowhere |
| `UNSUPPORTED_OBJECTIVE`, `UNSUPPORTED_TRIGGER`, `UNSUPPORTED_ACTION`, `UNSUPPORTED_EFFECT` | kinds Studio does not know, or an action used where it is not allowed |
| `DUPLICATE_SLOT`, `BOT_SLOT_UNAVAILABLE`, `INVALID_TEAM` | AI slot claimed twice or beyond the runtime's slots; team out of range |
| `MISSING_MODEL`, `MISSING_IMPORT`, `QUEENLORD_UNRESOLVED`, `MISSING_ICON` | a model, imported resource, Queenlord model or icon is not available |
| `INVALID_FIELD`, `INVALID_ID`, `LIMIT`, `EMPTY_GROUP`, `FUTURE_SCENARIO` | malformed values |

A game resource the loaded catalogs cannot resolve is an error; Studio never
substitutes a stand-in for it.

## Game models offered by role

`core/game_models.py` lists Global Assault models Studio offers by role
(tank, Queenlord, team base and its damage states, power-up pad, crates,
destructible props and their wrecks, barriers), each with an evidence class:
**proven** (a decoded record names the model in that role), **inferred**
(from how the game uses it plus how it looks) or **visual** (appearance
only). Only content IDs are listed; models resolve from the user's own
catalog. The Queenlord model is inferred: the multiplayer base record looks
it up only in game mode 11, Global Assault's multiplayer objectives keep
Queenlords at the base, and the model is a woman's portrait card.
