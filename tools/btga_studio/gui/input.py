"""Viewport input mapping as a pure function, so it can be tested without a window.

The GUI copies Dear ImGui's IO into an ``InputState`` every frame and
executes the returned commands.  Key bindings are configurable
(``DEFAULT_BINDINGS``, overridden from preferences).  Nothing fires while a
text field has keyboard focus (``text_input``); viewport commands need the
viewport hovered or focused.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# action -> key name; Ctrl combinations are fixed editor shortcuts.
DEFAULT_BINDINGS = {
    "forward": "W", "back": "S", "left": "A", "right": "D", "down": "Q", "up": "E",
    "frame_selection": "F", "frame_map": "Home", "cancel": "Escape", "delete": "Delete",
    "gizmo_translate": "1", "gizmo_rotate": "2", "gizmo_scale": "3", "gizmo_select": "4",
    "hide": "H", "isolate": "I", "lock": "L", "unhide_all": "U",
}
BINDABLE_KEYS = tuple("ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789") + ("Home", "End", "Escape", "Delete", "Insert", "PageUp", "PageDown",
                                                                     "F1", "F2", "F3", "F4", "F5", "F6", "F7", "F8")
CTRL_SHORTCUTS = {"Z": "undo", "Y": "redo", "S": "save", "D": "duplicate", "O": "open_project", "C": "copy", "V": "paste",
                  "N": "new_project", "A": "select_all"}


def validate_bindings(bindings: dict) -> dict:
    """Merged bindings; raises ValueError for unknown actions or keys, or one key bound twice."""

    merged = dict(DEFAULT_BINDINGS)
    for action, key in bindings.items():
        if action not in DEFAULT_BINDINGS:
            raise ValueError(f"unknown action {action!r}")
        if key not in BINDABLE_KEYS:
            raise ValueError(f"{key!r} cannot be bound")
        merged[action] = key
    keys = list(merged.values())
    duplicates = sorted({k for k in keys if keys.count(k) > 1})
    if duplicates:
        raise ValueError(f"keys bound to more than one action: {', '.join(duplicates)}")
    return merged


@dataclass
class InputState:
    dt: float = 1 / 60
    viewport_hovered: bool = False
    viewport_focused: bool = False
    text_input: bool = False
    gizmo_active: bool = False
    keys_down: set[str] = field(default_factory=set)  # held keys plus "Shift", "Ctrl", "Alt"
    keys_pressed: set[str] = field(default_factory=set)  # keys pressed this frame
    mouse_delta: tuple[float, float] = (0.0, 0.0)
    wheel: float = 0.0
    right_down: bool = False
    left_clicked: bool = False  # released without dragging
    box: tuple[float, float, float, float] | None = None  # finished drag rectangle (x0, y0, x1, y1)
    mouse: tuple[float, float] = (0.0, 0.0)
    bindings: dict = field(default_factory=lambda: dict(DEFAULT_BINDINGS))


@dataclass
class Command:
    name: str
    args: dict = field(default_factory=dict)


def commands(state: InputState) -> list[Command]:
    out: list[Command] = []
    if state.text_input:
        return out  # typing never moves the camera or edits the scene
    ctrl = "Ctrl" in state.keys_down
    shift = "Shift" in state.keys_down
    pressed = state.keys_pressed
    keys = state.bindings
    in_viewport = state.viewport_hovered or state.viewport_focused
    if ctrl:
        for key, name in CTRL_SHORTCUTS.items():
            if key in pressed:
                if name == "undo" and shift:
                    name = "redo"
                elif name == "save" and shift:
                    name = "save_as"
                out.append(Command(name))
        if state.left_clicked and state.viewport_hovered and not state.gizmo_active:
            out.append(Command("click", {"x": state.mouse[0], "y": state.mouse[1], "toggle": True}))
        return out
    if not in_viewport:
        return out
    for action in ("cancel", "delete", "frame_selection", "frame_map", "hide", "isolate", "lock", "unhide_all"):
        if keys[action] in pressed:
            out.append(Command(action))
    if not state.right_down:
        for action, mode in (("gizmo_select", "select"), ("gizmo_translate", "translate"), ("gizmo_rotate", "rotate"), ("gizmo_scale", "scale")):
            if keys[action] in pressed:
                out.append(Command("gizmo_mode", {"mode": mode}))
    if state.right_down:
        dx, dy = state.mouse_delta
        if dx or dy:
            out.append(Command("look", {"dx": dx, "dy": dy}))
    down = state.keys_down
    forward = (keys["forward"] in down) - (keys["back"] in down)
    strafe = (keys["right"] in down) - (keys["left"] in down)
    vertical = (keys["up"] in down) - (keys["down"] in down)
    if forward or strafe or vertical:
        out.append(Command("move", {"forward": forward, "strafe": strafe, "vertical": vertical, "fast": shift, "dt": state.dt}))
    if state.wheel and state.viewport_hovered:
        out.append(Command("speed", {"steps": state.wheel}))
    if state.box is not None and state.viewport_hovered and not state.gizmo_active:
        out.append(Command("box_select", {"rect": state.box, "add": shift}))
    elif state.left_clicked and state.viewport_hovered and not state.gizmo_active:
        out.append(Command("click", {"x": state.mouse[0], "y": state.mouse[1], "toggle": False}))
    return out
