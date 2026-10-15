"""Undo/redo for project-level gameplay definitions, map settings, minimap settings and prefabs.

Map-object edits keep their per-map transaction stacks (``EditorDocument``).
Everything else a user edits in the Gameplay editor goes through one
``DefinitionHistory`` per project:

* ``edit(label, mutate, ...)`` snapshots the definition state, runs
  ``mutate`` and snapshots again.  If ``mutate`` raises (invalid input), the
  state is restored and nothing enters history.  If nothing changed, nothing
  is recorded.
* ``coalesce`` merges consecutive edits with the same key (typing in one text
  box, dragging one slider) into one transaction until ``seal()`` is called
  (the widget lost focus or the drag ended) or ``COALESCE_SECONDS`` passes.
* Every transaction, here and in map documents, takes a number from one
  shared, increasing sequence (``next_sequence``), so a single Undo command
  can always reverse the most recent action whichever history it is in,
  without the two histories ever touching each other's data.

The state covers project title and description, campaigns, missions,
multiplayer, minigames, tanks, gangs, each map's title and minimap settings,
and the size and sky colour of original maps.  Unknown keys and imported
source data are never part of it, so undo cannot alter them.  Prefab files
are recorded as before/after file contents.
"""

from __future__ import annotations

import copy
import itertools
import json
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from .atomic import atomic_write_text

COALESCE_SECONDS = 2.0
LIMIT = 200
SAVED_LIMIT = 50
_lock = threading.Lock()
_counter = itertools.count(1)
_highest = [0]


def next_sequence() -> int:
    with _lock:
        value = max(next(_counter), _highest[0] + 1)
        _highest[0] = value
        return value


def observe(sequence: int) -> None:
    """Make sure future numbers are larger than one loaded from a saved project."""

    with _lock:
        _highest[0] = max(_highest[0], int(sequence))


FAMILIES = ("campaigns", "missions", "multiplayer", "minigames", "tanks", "gangs")


def capture(project) -> dict:
    state = {"project": {"title": project.project.get("title"), "description": project.project.get("description", "")}}
    for family in FAMILIES:
        state[family] = copy.deepcopy(getattr(project, family))
    maps = {}
    for key, entry in sorted(project.maps.items()):
        item = {"title": entry.title, "minimap": copy.deepcopy(entry.minimap)}
        if entry.kind == "blank" and entry.base:
            item["bounds"] = copy.deepcopy(entry.base["bounds"]["map"])
            item["colour"] = list(entry.base["environment"][0]["clear_and_fog_colour"])
        maps[key] = item
    state["maps"] = maps
    return state


def restore(project, state: dict) -> None:
    project.project["title"] = state["project"]["title"]
    project.project["description"] = state["project"]["description"]
    for family in FAMILIES:
        setattr(project, family, copy.deepcopy(state[family]))
    for key, item in state["maps"].items():
        entry = project.maps.get(key)
        if entry is None:
            continue
        entry.title = item["title"]
        entry.minimap = copy.deepcopy(item["minimap"])
        if "bounds" in item and entry.base:
            entry.base["bounds"]["map"] = copy.deepcopy(item["bounds"])
            entry.base["environment"][0]["clear_and_fog_colour"] = list(item["colour"])
            if entry.document is not None:
                entry.document.scene.base["bounds"] = copy.deepcopy(entry.base["bounds"])
                entry.document.scene.base["environment"] = copy.deepcopy(entry.base["environment"])


@dataclass
class Entry:
    label: str
    before: dict | None
    after: dict | None
    sequence: int
    context: dict = field(default_factory=dict)
    coalesce: str | None = None
    stamp: float = 0.0
    files: list = field(default_factory=list)  # [(path, before_text | None, after_text | None)] for prefab files

    def as_dict(self) -> dict:
        return {"label": self.label, "before": self.before, "after": self.after, "sequence": self.sequence, "context": self.context}


class DefinitionHistory:
    def __init__(self, project):
        self.project = project
        self.undo_stack: list[Entry] = []
        self.redo_stack: list[Entry] = []
        self._open: Entry | None = None  # the transaction still accepting coalesced edits

    # -- recording ---------------------------------------------------------------------
    def edit(self, label: str, mutate, coalesce: str | None = None, context: dict | None = None) -> bool:
        before = capture(self.project)
        try:
            mutate()
        except BaseException:
            restore(self.project, before)
            raise
        after = capture(self.project)
        if after == before:
            return False
        now = time.monotonic()
        current = self._open
        if coalesce and current is not None and current.coalesce == coalesce and self.undo_stack and self.undo_stack[-1] is current \
                and now - current.stamp <= COALESCE_SECONDS:
            current.after, current.stamp = after, now
            self.redo_stack.clear()
            return True
        entry = Entry(label, before, after, next_sequence(), dict(context or {}), coalesce, now)
        self._push(entry)
        self._open = entry if coalesce else None
        return True

    def record_file(self, label: str, path: Path, before: str | None, after: str | None, context: dict | None = None) -> None:
        """A prefab file was written or deleted (the caller already did it); record it for undo."""

        self._push(Entry(label, None, None, next_sequence(), dict(context or {}), None, time.monotonic(), [(Path(path), before, after)]))
        self._open = None

    def _push(self, entry: Entry) -> None:
        self.undo_stack.append(entry)
        del self.undo_stack[:-LIMIT]
        self.redo_stack.clear()

    def seal(self) -> None:
        """End coalescing (text box lost focus, slider released)."""

        self._open = None

    # -- undo / redo -----------------------------------------------------------------------
    @property
    def undo_sequence(self) -> int | None:
        return self.undo_stack[-1].sequence if self.undo_stack else None

    @property
    def redo_sequence(self) -> int | None:
        return self.redo_stack[-1].sequence if self.redo_stack else None

    def undo(self) -> Entry | None:
        self._open = None
        if not self.undo_stack:
            return None
        entry = self.undo_stack.pop()
        self._apply(entry, backwards=True)
        self.redo_stack.append(entry)
        return entry

    def redo(self) -> Entry | None:
        self._open = None
        if not self.redo_stack:
            return None
        entry = self.redo_stack.pop()
        self._apply(entry, backwards=False)
        self.undo_stack.append(entry)
        return entry

    def _apply(self, entry: Entry, backwards: bool) -> None:
        state = entry.before if backwards else entry.after
        if state is not None:
            restore(self.project, state)
        for path, before, after in entry.files:
            text = before if backwards else after
            if text is None:
                Path(path).unlink(missing_ok=True)
            else:
                atomic_write_text(Path(path), text)

    # -- persistence ------------------------------------------------------------------------
    def as_dict(self) -> dict:
        """Project-state transactions only (prefab files are per user, not per project); bounded and deterministic."""

        keep = lambda stack: [e.as_dict() for e in stack if e.before is not None][-SAVED_LIMIT:]  # noqa: E731
        return {"undo": keep(self.undo_stack), "redo": keep(self.redo_stack)}

    def load(self, data) -> None:
        if not isinstance(data, dict):
            return
        stacks = []
        for name in ("undo", "redo"):
            entries = []
            for item in data.get(name, []) if isinstance(data.get(name), list) else []:
                if not (isinstance(item, dict) and isinstance(item.get("before"), dict) and isinstance(item.get("after"), dict)
                        and isinstance(item.get("sequence"), int)):
                    continue
                entries.append(Entry(str(item.get("label", "Edit")), item["before"], item["after"], item["sequence"],
                                     item.get("context") if isinstance(item.get("context"), dict) else {}))
                observe(item["sequence"])
            stacks.append(entries)
        self.undo_stack, self.redo_stack = stacks
        self._open = None


def canonical(state: dict) -> str:
    return json.dumps(state, sort_keys=True)
