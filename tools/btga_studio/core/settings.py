"""Application data locations and editor preferences (kept apart from project data).

Per-user locations on Windows (nothing is written next to the program):

    %APPDATA%\\BattleTanx Studio        preferences.json, layout.ini, sources.json (small, roams)
    %LOCALAPPDATA%\\BattleTanx Studio   catalogs\\, thumbnails\\, starter\\, recovery\\, logs\\, prefabs\\ (large, machine local)
    Documents\\BattleTanx Studio Projects   default folder for new projects

Linux uses ``$XDG_CONFIG_HOME/battletanx-studio`` and ``$XDG_DATA_HOME/battletanx-studio``.

Portable mode keeps both under ``BattleTanxStudioData`` beside the program; it
is selected by ``--portable`` or a ``portable.txt`` file next to the executable.
The developer/test override ``BTGA_STUDIO_HOME`` names one directory for both.

Data written by earlier builds under ``%APPDATA%\\BTGAStudio`` (or
``~/.config/btga-studio``) is migrated once: preferences and the source
registry are copied; catalogs are rebuilt on demand.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .atomic import atomic_write_text

APP_NAME = "BattleTanx Studio"
PREFERENCES_VERSION = 1
MAX_RECENT = 12
LEGACY_NAMES = ("BTGAStudio", "btga-studio")


def program_directory() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[1]


def bundled_data_directory() -> Path:
    """Read-only data shipped with the program (PyInstaller unpacks it under ``sys._MEIPASS``)."""

    base = getattr(sys, "_MEIPASS", None)
    return Path(base) / "data" if base else program_directory() / "data"


def is_portable(portable: bool = False) -> bool:
    return portable or (program_directory() / "portable.txt").is_file()


def data_directories(portable: bool = False) -> tuple[Path, Path]:
    """(roaming configuration directory, local data directory)."""

    override = os.environ.get("BTGA_STUDIO_HOME")
    if override:
        return Path(override), Path(override)
    if is_portable(portable):
        root = program_directory() / "BattleTanxStudioData"
        return root, root
    if sys.platform == "win32":
        roaming = Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming")
        local = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
        return roaming / APP_NAME, local / APP_NAME
    config = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    data = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")
    return config / "battletanx-studio", data / "battletanx-studio"


def data_directory(portable: bool = False) -> Path:
    return data_directories(portable)[0]


def legacy_directory() -> Path | None:
    if os.environ.get("BTGA_STUDIO_HOME"):
        return None
    if sys.platform == "win32":
        base = Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming")
        return base / LEGACY_NAMES[0]
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / LEGACY_NAMES[1]


def default_projects_directory() -> Path:
    override = os.environ.get("BTGA_STUDIO_HOME")
    if override:
        return Path(override) / "Projects"
    documents = Path.home() / "Documents"
    return (documents if documents.is_dir() else Path.home()) / f"{APP_NAME} Projects"


@dataclass
class Preferences:
    version: int = PREFERENCES_VERSION
    last_catalog: str | None = None
    recent_projects: list[str] = field(default_factory=list)
    pinned_projects: list[str] = field(default_factory=list)
    projects_directory: str | None = None
    welcome_seen: bool = False
    show_hub_at_start: bool = True
    tutorial_step: int = 0
    tutorial_active: bool = False
    tutorial_done: bool = False
    camera_speed: float = 1500.0
    grid_snap: float = 0.0
    yaw_snap_degrees: int = 0
    surface_snap: bool = True
    gizmo_local: bool = False
    wireframe: bool = False
    layers: dict = field(default_factory=lambda: {"geometry": True, "collision": True, "markers": True, "navigation": True,
                                                   "bounds": True, "minimap": False, "unresolved": True, "grid": True})
    autosave_enabled: bool = True
    autosave_seconds: int = 120
    thumbnail_cache_mib: int = 64
    thumbnail_workers: int = 2
    browser_thumbnails: bool = True
    bindings: dict = field(default_factory=dict)  # action -> key overrides (gui.input.DEFAULT_BINDINGS)
    look_sensitivity: float = 0.0035
    invert_look_y: bool = False
    # Test in Game: kept on this computer only, never written into projects or packages.
    runtime_executable: str | None = None
    runtime_rom: str | None = None
    runtime_capture: str = "none"

    def add_recent(self, path: str) -> None:
        self.recent_projects = [path] + [p for p in self.recent_projects if p != path]
        pinned = [p for p in self.recent_projects if p in self.pinned_projects]
        others = [p for p in self.recent_projects if p not in self.pinned_projects][: max(0, MAX_RECENT - len(pinned))]
        self.recent_projects = [p for p in self.recent_projects if p in pinned or p in others]

    def remove_recent(self, path: str) -> None:
        self.recent_projects = [p for p in self.recent_projects if p != path]
        self.pinned_projects = [p for p in self.pinned_projects if p != path]

    def replace_recent(self, old: str, new: str) -> None:
        """A recent project was found at a new location."""

        self.recent_projects = [new if p == old else p for p in self.recent_projects]
        self.pinned_projects = [new if p == old else p for p in self.pinned_projects]
        self.add_recent(new)

    def set_pinned(self, path: str, pinned: bool) -> None:
        self.pinned_projects = [p for p in self.pinned_projects if p != path] + ([path] if pinned else [])
        if path not in self.recent_projects:
            self.add_recent(path)


class Settings:
    def __init__(self, directory: Path, local_directory: Path | None = None, portable: bool = False):
        self.directory = Path(directory)
        self.local_directory = Path(local_directory) if local_directory else self.directory
        self.portable = portable
        self.preferences = Preferences()
        self.migrated_from: str | None = None

    @classmethod
    def open(cls, portable: bool = False) -> "Settings":
        roaming, local = data_directories(portable)
        settings = cls(roaming, local, is_portable(portable))
        if not settings.portable and not settings.preferences_file.exists():
            settings.migrate(legacy_directory())
        settings.load()
        return settings

    # -- locations ---------------------------------------------------------------------
    @property
    def preferences_file(self) -> Path:
        return self.directory / "preferences.json"

    @property
    def layout_file(self) -> Path:
        return self.directory / "layout.ini"

    @property
    def thumbnail_directory(self) -> Path:
        return self.local_directory / "thumbnails"

    @property
    def recovery_directory(self) -> Path:
        return self.local_directory / "recovery"

    @property
    def starter_directory(self) -> Path:
        return self.local_directory / "starter"

    @property
    def logs_directory(self) -> Path:
        return self.local_directory / "logs"

    @property
    def prefabs_directory(self) -> Path:
        return self.local_directory / "prefabs"

    @property
    def projects_directory(self) -> Path:
        chosen = self.preferences.projects_directory
        return Path(chosen) if chosen else default_projects_directory()

    def locations(self) -> list[tuple[str, Path]]:
        return [("Settings", self.directory), ("Local data (catalogs, caches)", self.local_directory), ("Logs", self.logs_directory),
                ("Thumbnail cache", self.thumbnail_directory), ("Recovery copies", self.recovery_directory),
                ("Projects (default)", self.projects_directory)]

    # -- persistence ----------------------------------------------------------------------
    def migrate(self, legacy: Path | None) -> bool:
        """Copy preferences and the source registry from an earlier build's folder (once)."""

        if legacy is None or not legacy.is_dir() or legacy.resolve() == self.directory.resolve():
            return False
        copied = False
        for name in ("preferences.json", "layout.ini", "sources.json"):
            source = legacy / name
            if source.is_file() and not (self.directory / name).exists():
                self.directory.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, self.directory / name)
                copied = True
        if copied:
            self.migrated_from = legacy.name
        return copied

    def load(self) -> None:
        try:
            data = json.loads(self.preferences_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if not isinstance(data, dict):
            return
        defaults = Preferences()
        for key, value in data.items():
            if key not in Preferences.__dataclass_fields__:
                continue
            default = getattr(defaults, key)
            if isinstance(default, bool) != isinstance(value, bool):
                continue
            if default is None or isinstance(value, type(default)) or (isinstance(default, float) and isinstance(value, int)):
                setattr(self.preferences, key, value)
        prefs = self.preferences
        prefs.recent_projects = [p for p in prefs.recent_projects if isinstance(p, str)][: MAX_RECENT * 2]
        prefs.pinned_projects = [p for p in prefs.pinned_projects if isinstance(p, str)]

    def save(self) -> None:
        atomic_write_text(self.preferences_file, json.dumps(asdict(self.preferences), indent=1, sort_keys=True) + "\n")
