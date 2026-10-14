"""Application data locations and editor preferences (kept apart from project data).

Portable mode stores everything next to the program (``BTGAStudioData``)
and is selected by ``--portable``, by a ``portable.txt`` file beside the
executable, or by the ``BTGA_STUDIO_HOME`` environment variable (which
names the data directory directly; tests always use it, so they never touch
the user's real settings).
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path

PREFERENCES_VERSION = 1
MAX_RECENT = 10


def program_directory() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[1]


def data_directory(portable: bool = False) -> Path:
    override = os.environ.get("BTGA_STUDIO_HOME")
    if override:
        return Path(override)
    if portable or (program_directory() / "portable.txt").is_file():
        return program_directory() / "BTGAStudioData"
    if sys.platform == "win32":
        return Path(os.environ.get("APPDATA", Path.home())) / "BTGAStudio"
    return Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "btga-studio"


@dataclass
class Preferences:
    version: int = PREFERENCES_VERSION
    last_catalog: str | None = None
    recent_projects: list[str] = field(default_factory=list)
    camera_speed: float = 1500.0
    grid_snap: float = 0.0
    yaw_snap_degrees: int = 0
    surface_snap: bool = True
    gizmo_local: bool = False
    wireframe: bool = False
    layers: dict = field(default_factory=lambda: {"geometry": True, "collision": True, "markers": True, "navigation": True,
                                                   "bounds": True, "minimap": False, "unresolved": True, "grid": True})
    autosave_enabled: bool = False
    autosave_seconds: int = 120
    thumbnail_cache_mib: int = 64
    thumbnail_workers: int = 2
    browser_thumbnails: bool = True
    bindings: dict = field(default_factory=dict)  # action -> key overrides (gui.input.DEFAULT_BINDINGS)
    look_sensitivity: float = 0.0035
    invert_look_y: bool = False

    def add_recent(self, path: str) -> None:
        self.recent_projects = [path] + [p for p in self.recent_projects if p != path]
        del self.recent_projects[MAX_RECENT:]


class Settings:
    def __init__(self, directory: Path):
        self.directory = Path(directory)
        self.preferences = Preferences()

    @classmethod
    def open(cls, portable: bool = False) -> "Settings":
        settings = cls(data_directory(portable))
        settings.load()
        return settings

    @property
    def preferences_file(self) -> Path:
        return self.directory / "preferences.json"

    @property
    def layout_file(self) -> Path:
        return self.directory / "layout.ini"

    @property
    def thumbnail_directory(self) -> Path:
        return self.directory / "thumbnails"

    @property
    def recovery_directory(self) -> Path:
        return self.directory / "recovery"

    @property
    def starter_directory(self) -> Path:
        return self.directory / "starter"

    def load(self) -> None:
        try:
            data = json.loads(self.preferences_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        known = {k: v for k, v in data.items() if k in Preferences.__dataclass_fields__}
        defaults = Preferences()
        for key, value in known.items():
            if isinstance(value, type(getattr(defaults, key))) or getattr(defaults, key) is None:
                setattr(self.preferences, key, value)

    def save(self) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        temporary = self.preferences_file.with_suffix(".tmp")
        temporary.write_text(json.dumps(asdict(self.preferences), indent=1, sort_keys=True) + "\n", encoding="utf-8")
        os.replace(temporary, self.preferences_file)
