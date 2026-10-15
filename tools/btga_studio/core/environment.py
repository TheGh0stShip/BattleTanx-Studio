"""Where and how this copy of Studio is running: installed, portable or from source.

``describe(settings)`` returns plain facts for the About dialog and Settings:
mode, version, build revision, program folder, install record version,
user-data folders, and repair guidance.  Nothing here writes anything.

* ``installed``: the program folder holds an ``install-record.json`` with
  Studio's upgrade code (written by the installer).
* ``portable``: a frozen program without that record (an unzipped release).
* ``source``: running from a source checkout (developers).
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from pathlib import Path

from tools.btga_studio import __version__

from . import installer
from .settings import bundled_data_directory, program_directory


@dataclass(frozen=True)
class Environment:
    mode: str  # installed | portable | source
    version: str
    revision: str
    program: Path
    installed_version: str | None
    portable_data: bool

    @property
    def label(self) -> str:
        return {"installed": "Installed for this user", "portable": "Portable (running from a folder)",
                "source": "Development (running from source)"}[self.mode]

    @property
    def repair_guidance(self) -> str:
        if self.mode == "installed":
            return ("If Studio stops starting or files are missing, run BattleTanxStudioSetup.exe for this version again and choose "
                    "Repair. Your projects and settings are kept.")
        if self.mode == "portable":
            return "If files are missing, unzip the portable download again into a new folder. Your projects and settings are kept."
        return "Running from source: reinstall the requirements to repair."


def build_information() -> dict:
    try:
        data = json.loads((bundled_data_directory() / "build.json").read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def detect(program: Path | None = None, frozen: bool | None = None) -> Environment:
    program = Path(program) if program else program_directory()
    frozen = getattr(sys, "frozen", False) if frozen is None else frozen
    record = installer.inspect(program, _NoRegistry()).record if frozen else None
    mode = "installed" if record is not None else ("portable" if frozen else "source")
    info = build_information()
    return Environment(mode, __version__, str(info.get("revision") or ("source checkout" if not frozen else "unknown")), program,
                       record.get("version") if record else None, (program / "portable.txt").is_file())


class _NoRegistry:
    def read_registration(self):
        return None
