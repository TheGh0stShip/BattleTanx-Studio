"""Hand a folder, file or web page to the operating system (Explorer / default browser).

Nothing runs a shell or shows a console: Windows uses ``os.startfile``;
other platforms start ``xdg-open`` / ``open`` detached with no terminal.
Only http(s) URLs on an allow-list are opened.  Tests replace ``LAUNCHER``
to record requests instead of opening anything.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

RELEASES_URL = "https://github.com/TheGh0stShip/BattleTanx-Studio/releases"
ISSUES_URL = "https://github.com/TheGh0stShip/BattleTanx-Studio/issues/new"
ALLOWED_PREFIXES = ("https://github.com/TheGh0stShip/BattleTanx-Studio",)


def _launch(target: str) -> None:
    if sys.platform == "win32":
        os.startfile(target)  # noqa: S606 (Explorer or the default browser; no console)
    else:
        command = "open" if sys.platform == "darwin" else "xdg-open"
        subprocess.Popen([command, target], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True)


LAUNCHER = _launch


def open_folder(path: Path) -> Path:
    """Show a folder (created if needed); for a file, show the folder containing it."""

    path = Path(path)
    folder = path if path.is_dir() or not path.suffix else path.parent
    folder.mkdir(parents=True, exist_ok=True)
    LAUNCHER(str(folder))
    return folder


def open_url(url: str) -> str:
    if not url.startswith(ALLOWED_PREFIXES):
        raise ValueError("only BattleTanx Studio project pages can be opened")
    LAUNCHER(url)
    return url
