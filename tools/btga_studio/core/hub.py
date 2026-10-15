"""Project Hub operations: templates, creation, recent projects, duplicate, rename and relink.

Everything here works on project files and preferences only, without a
window, so the Hub and the tests share one implementation.

* ``TEMPLATES`` describe the starting points offered to users; ``create``
  builds one from Studio starter content (no game data) and saves it at once,
  so a new project always has a file.
* ``recent_entries`` reports, for each recent project, whether the file
  exists, its title and any missing dependencies (sources or models that are
  not available), before the user opens it.
* ``duplicate`` writes a copy with a new project identity (so autosave and
  recovery never confuse the two); ``rename`` changes the title and, when
  asked, the file name, writing the new file before removing the old one.
* ``relink`` accepts a moved project file only if it is the same project
  (same identity), then updates the recent list.

Paths stay local: nothing here writes a path into a project.
"""

from __future__ import annotations

import re
import secrets
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

from . import project as projects

PROJECT_SUFFIX = ".btgaproj"
MAP_SIZES = {"Small (2,048 units across)": 1024.0, "Medium (4,096 units across)": 2048.0, "Large (8,192 units across)": 4096.0}
MAX_PEEK_BYTES = 64 * 1024 * 1024


@dataclass(frozen=True)
class Template:
    key: str
    title: str
    description: str
    multiplayer: bool = False


TEMPLATES = (
    Template("blank", "Blank map", "An empty map with ground tiles. Build anything you like."),
    Template("skirmish", "Multiplayer arena", "A square arena with team starts, cover walls and a pickup, plus a multiplayer definition.",
             multiplayer=True),
    Template("mission", "Single-player mission", "A player spawn, a target to destroy, an objective, and a one-mission campaign."),
    Template("example", "Starter example", "Three finished example maps with a campaign, multiplayer, a minigame, tanks and gangs."),
)
MODES = ("deathmatch", "team deathmatch")


class HubError(ValueError):
    pass


def slug(text: str, fallback: str = "map") -> str:
    """A valid map or definition key derived from user text (users never type keys)."""

    ascii_text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    key = re.sub(r"[^a-z0-9]+", "_", ascii_text.lower()).strip("_")[:40] or fallback
    if not key[0].isalnum():
        key = f"{fallback}_{key}"
    return key


def safe_file_stem(title: str) -> str:
    stem = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", title).strip().rstrip(".")
    return stem[:80] or "Untitled project"


def unique_path(directory: Path, title: str) -> Path:
    directory = Path(directory)
    stem = safe_file_stem(title)
    candidate = directory / f"{stem}{PROJECT_SUFFIX}"
    number = 2
    while candidate.exists():
        candidate = directory / f"{stem} ({number}){PROJECT_SUFFIX}"
        number += 1
    return candidate


# ------------------------------------------------------------------------------------------ create --

def _tiles(document, models, half: float) -> None:
    step = 512.0
    count = int(half * 2 // step)
    origin = -count * step / 2
    for x in range(count):
        for z in range(count):
            document.place_model(models["ground_tile"], (origin + x * step + step / 2, 0.0, origin + z * step + step / 2))


def create(library, title: str, template: str = "blank", half_extent: float = 2048.0, players: int = 4, mode: str = "deathmatch",
           directory: Path | None = None, path: Path | None = None) -> tuple[Path, projects.StudioProject]:
    """Create a project from a template using only Studio starter content, save it, and return (path, project)."""

    projects._text(title, "title")
    if template not in {t.key for t in TEMPLATES}:
        raise HubError(f"unknown template {template!r}")
    if not 2 <= int(players) <= 4:
        raise HubError("players must be between 2 and 4")
    if mode not in MODES:
        raise HubError(f"unknown mode {mode!r}")
    starter = library.starter()
    if starter is None:
        raise HubError("the built-in starter content is not available")
    if template == "example":
        from tools.btga_studio.content.examples import build_example

        project = build_example(library)
        project.project["id"] = secrets.token_hex(8)
        project.project["title"] = title
    else:
        project = projects.StudioProject(title, "studio")
        models = starter["models"]
        templates = {t["source_kind"].removeprefix("studio_"): t for t in starter["templates"]}
        key = slug(title)
        project.add_blank_map(key, title, half_extent)
        document = project.open_map(key, library)
        _tiles(document, models, min(half_extent, 2048.0))
        inner = min(half_extent, 2048.0) * 0.75
        if template == "skirmish":
            corners = ((-inner, -inner, 0x2000), (inner, -inner, 0xE000), (-inner, inner, 0x6000), (inner, inner, 0xA000))
            for team, (x, z, yaw) in enumerate(corners[: int(players)]):
                document.place_marker(templates["team_start"], (x, 0.0, z), yaw, values={"team": team})
            for x in (-inner / 3, inner / 3):
                document.place_model(models["wall"], (x, 0.0, 0.0), 0x4000)
            document.place_marker(templates["pickup"], (0.0, 0.0, 0.0))
            project.define("multiplayer", key, {"title": title, "map": key, "modes": [mode], "max_players": int(players)})
        elif template == "mission":
            document.place_marker(templates["player_spawn"], (0.0, 0.0, inner), 0x8000)
            target = document.place_model(models["cube"], (0.0, 0.0, -inner))
            objective = document.place_marker(templates["objective"], (0.0, 0.0, -inner + 128.0),
                                              values={"target": target, "label": "Destroy the target"})
            project.define("missions", key, {"title": title, "map": key, "objectives": [objective], "briefing": ""})
            project.campaigns.append({"id": f"{key}_campaign", "title": f"{title} campaign", "missions": [key]})
        document.selection = []
    project.compute_dependencies(library)
    target = Path(path) if path else unique_path(directory or Path.cwd(), title)
    projects.save_project(target, project)
    return target, project


# ------------------------------------------------------------------------------------------ recents --

@dataclass
class RecentEntry:
    path: Path
    title: str
    exists: bool
    pinned: bool
    modified: float | None = None
    warnings: list[str] = field(default_factory=list)
    error: str | None = None
    project_id: str | None = None


def peek(path: Path, library=None) -> RecentEntry:
    """Read a project's title and dependency problems without opening any map."""

    path = Path(path)
    if not path.is_file():
        return RecentEntry(path, path.stem, False, False, error="The file is no longer at this location. Use Locate to find it.")
    stat = path.stat()
    entry = RecentEntry(path, path.stem, True, False, stat.st_mtime)
    if stat.st_size > MAX_PEEK_BYTES:
        entry.warnings.append("Large project; details are checked when it opens.")
        return entry
    try:
        project = projects.loads_project(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, projects.ProjectError) as error:
        entry.error = f"This file cannot be opened: {error}"
        return entry
    entry.title, entry.project_id = project.project.get("title") or path.stem, project.project.get("id")
    if library is not None:
        for problem in project.problems(library):
            if problem["code"] in ("MISSING_SOURCE", "MISSING_MODEL"):
                entry.warnings.append(problem["message"])
    return entry


def recent_entries(preferences, library=None) -> list[RecentEntry]:
    entries = []
    pinned = set(preferences.pinned_projects)
    ordered = [p for p in preferences.recent_projects if p in pinned] + [p for p in preferences.recent_projects if p not in pinned]
    for text in ordered:
        entry = peek(Path(text), library)
        entry.pinned = text in pinned
        entries.append(entry)
    return entries


def relink(preferences, old: Path, new: Path, expected_id: str | None = None) -> Path:
    """Point a recent entry at a moved project file after checking it is the same project."""

    entry = peek(Path(new))
    if entry.error:
        raise HubError(entry.error)
    if expected_id and entry.project_id != expected_id:
        raise HubError("That file is a different project. Choose the moved copy of this project.")
    preferences.replace_recent(str(old), str(new))
    return Path(new)


# ------------------------------------------------------------------------------- duplicate / rename --

def duplicate(path: Path, title: str | None = None) -> Path:
    project = projects.load_project(Path(path))
    project.project["id"] = secrets.token_hex(8)
    project.project["title"] = projects._text(title or f"{project.project.get('title', 'Project')} (copy)", "title")
    target = unique_path(Path(path).parent, project.project["title"])
    projects.save_project(target, project)
    return target


def rename(path: Path, title: str, rename_file: bool = True) -> Path:
    """Change a closed project's title (and file name).  The new file is complete before the old one is removed."""

    path = Path(path)
    project = projects.load_project(path)
    project.project["title"] = projects._text(title, "title")
    target = path
    if rename_file:
        target = path.with_name(safe_file_stem(title) + PROJECT_SUFFIX)
        if target != path and target.exists():
            raise HubError(f"A file named {target.name} already exists in this folder.")
    projects.save_project(target, project)
    if target != path:
        path.unlink()
    return target
