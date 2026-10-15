"""Guided first-project tour: steps, completion checks and per-user progress.

Pure logic, no window: the GUI computes ``facts`` from the editor each frame
(see ``StudioApp.tutorial_facts``) and calls ``update``.  A step is complete
when its check sees the user's action relative to the facts recorded when
the step began (``baseline``), so earlier work never completes a later step.

Progress (step number, active, finished) is stored in the user's preferences,
never in a project.  The tour never blocks editing: every step can be
skipped, the tour can be closed and restarted from Help, and nothing it
shows is modal.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Step:
    key: str
    title: str
    text: str
    anchor: str | None  # control to point at; None = no specific control
    hidden_hint: str  # what to do when that control is not on screen


def _more(name):
    return lambda facts, base: facts.get(name, 0) > base.get(name, 0)


STEPS = (
    Step("open", "Open a project", "Open the starter example (button below) or create your own project. It uses only Studio's own content.",
         None, ""),
    Step("look", "Look around", "Hold the right mouse button over the 3D view and move the mouse to look. W A S D move, Q and E go down "
         "and up, the mouse wheel changes speed.", "viewport", "Close other windows so the 3D view is visible."),
    Step("select", "Select something", "Left-click any object in the 3D view. Its details appear in the Inspector on the right.",
         "viewport", "Close other windows so the 3D view is visible."),
    Step("move", "Move it", "Choose Move in the toolbar, then drag one of the coloured arrows. Ctrl+Z undoes it.", "toolbar.translate",
         "Open a map so the toolbar appears above the 3D view."),
    Step("spawn", "Place a spawn point", "In the Assets panel (bottom) click \"player spawn\", then click on the ground. Esc stops placing.",
         "assets", "The Assets panel is hidden. Use View > Reset window layout to bring it back."),
    Step("pickup", "Place a pickup", "Click \"pickup\" in the Assets panel, then click on the ground.", "assets",
         "The Assets panel is hidden. Use View > Reset window layout to bring it back."),
    Step("objective", "Add an objective", "Click \"objective\" in the Assets panel and place it. Then set its target in the Inspector.",
         "assets", "The Assets panel is hidden. Use View > Reset window layout to bring it back."),
    Step("settings", "Map and gameplay settings", "Open the Gameplay editor (left panel). It holds map size, missions, multiplayer and more.",
         "gameplay.open", "Open the Project panel on the left (View > Reset window layout)."),
    Step("validate", "Check your map", "The Validation tab (bottom right) lists problems as you work; click one to jump to it. "
         "Press Check now there. This step completes when no errors remain.", "validation",
         "Use View > Reset window layout to show the Validation tab."),
    Step("save", "Save", "Press Save in the toolbar (or Ctrl+S).", "toolbar.save", "Open a map so the toolbar appears."),
    Step("export", "Export a package", "Choose File > Export content package. The package holds only your work and Studio's own content.",
         None, ""),
)

CHECKS = {
    "open": lambda facts, base: bool(facts.get("project")),
    "look": lambda facts, base: facts.get("camera") is not None and facts.get("camera") != base.get("camera"),
    "select": lambda facts, base: facts.get("selection", 0) > 0,
    "move": _more("transforms"),
    "spawn": _more("spawns"),
    "pickup": _more("pickups"),
    "objective": _more("objectives"),
    "settings": lambda facts, base: bool(facts.get("gameplay_open")),
    "validate": lambda facts, base: facts.get("checks", 0) > base.get("checks", 0) and bool(facts.get("validated")),
    "save": lambda facts, base: bool(facts.get("saved")) and facts.get("saves", 0) > base.get("saves", 0),
    "export": _more("exports"),
}


class Tutorial:
    def __init__(self, preferences):
        self.preferences = preferences
        self.baseline: dict | None = None

    @property
    def active(self) -> bool:
        return bool(self.preferences.tutorial_active) and not self.finished

    @property
    def finished(self) -> bool:
        return self.preferences.tutorial_step >= len(STEPS)

    @property
    def step(self) -> Step | None:
        index = self.preferences.tutorial_step
        return STEPS[index] if self.preferences.tutorial_active and 0 <= index < len(STEPS) else None

    def start(self, from_beginning: bool = True) -> None:
        if from_beginning or self.finished:
            self.preferences.tutorial_step = 0
        self.preferences.tutorial_active = True
        self.baseline = None

    def stop(self) -> None:
        """Close the tour; progress is kept and Help > Guided tour continues or restarts it."""

        self.preferences.tutorial_active = False
        self.baseline = None

    def skip(self) -> None:
        self.advance()

    def back(self) -> None:
        self.preferences.tutorial_step = max(0, self.preferences.tutorial_step - 1)
        self.baseline = None

    def advance(self) -> None:
        self.preferences.tutorial_step = min(len(STEPS), self.preferences.tutorial_step + 1)
        self.baseline = None
        if self.finished:
            self.preferences.tutorial_active = False
            self.preferences.tutorial_done = True

    def update(self, facts: dict) -> bool:
        """Record the baseline on a step's first frame; advance when its check passes.  Returns True when a step completed."""

        step = self.step
        if step is None:
            return False
        if self.baseline is None:
            self.baseline = dict(facts)
            if step.key != "open":
                return False
        if CHECKS[step.key](facts, self.baseline):
            self.advance()
            return True
        return False
