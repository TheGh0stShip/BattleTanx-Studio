"""Capture release-experience screenshots off-screen: setup pages, uninstall, Unicode names, guided tour, undo, About.

Application framebuffers only (EGL); synthetic data only: a synthetic program
folder for the setup pages (nothing is installed outside a temporary
sandbox), Studio starter content for the editor.  Each image gets automatic
blank/flat checks and an adjacent JSON record tied to the source identity;
``--review`` merges reviewer verdicts (see ``capture_studio_acceptance.py``).

    python tools/capture_studio_release.py OUTPUT_DIR
    python tools/capture_studio_release.py OUTPUT_DIR --review VERDICTS.json
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools import studio_evidence as ev  # noqa: E402
from tools.capture_studio_acceptance import merge_review  # noqa: E402

UNICODE_NAMES = ("Café Crème Élan", "Ελληνική Αρένα", "Кириллица Карта", "日本語のマップ", "한국어 지도", "中文地图")


def synthetic_program(root: Path) -> Path:
    from tools.btga_studio.packaging import release

    dist = root / "BattleTanxStudio"
    for name in ("_internal/python313.dll", "_internal/data/build.json", "samples/starter-example.btgaproj"):
        (dist / name).parent.mkdir(parents=True, exist_ok=True)
        (dist / name).write_bytes(b"synthetic " + name.encode() * 64)
    (dist / "BattleTanxStudio.exe").write_bytes(b"MZ synthetic program")
    (dist / "README.txt").write_text("readme", encoding="utf-8")
    (dist / "COPYING.txt").write_text("BattleTanx Studio licence text (see COPYING in the source).", encoding="utf-8")
    release.prepare_dist(dist)
    return dist


def capture(output: Path) -> list[dict]:
    os.environ.setdefault("PYOPENGL_PLATFORM", "egl")
    identity = ev.source_identity(ROOT)
    output.mkdir(parents=True, exist_ok=True)
    results = []

    def record(surface, name, scenario, expected):
        width, height, rgba = surface.pixels()
        stats = ev.image_stats(width, height, rgba)
        problems = ev.inspect_image(stats)
        png = output / f"{name}.png"
        surface.screenshot(png)
        path = ev.write_record(png, identity, scenario, expected, f"automatic: {stats['unique_colours']} colours", not problems,
                               "automatic checks only; pending visual review" if not problems else "; ".join(problems), stats=stats)
        results.append({"name": name, "ok": not problems, "problems": problems, "record": path.name})

    with tempfile.TemporaryDirectory(prefix="release capture ") as temp:
        temp = Path(temp)
        os.environ["BTGA_STUDIO_HOME"] = str(temp / "home")
        setup_pages(temp, record)
        editor_pages(temp, record)
    return results


def setup_pages(temp: Path, record) -> None:
    from tools.btga_studio.core import installer
    from tools.btga_studio.gui.offscreen import OffscreenSurface
    from tools.btga_studio.setup.main import licences
    from tools.btga_studio.setup.ui import SetupUI, UninstallUI

    os.environ["LOCALAPPDATA"] = str(temp / "Local App Data")  # the sandbox stands in for the user's local app data
    dist = synthetic_program(temp / "payload")
    system = installer.SandboxSystem(temp / "sandbox", running=[])
    target = temp / "Local App Data" / "Programs" / "BattleTanx Studio"
    older = synthetic_program(temp / "older")
    manifest = json.loads((older / installer.PAYLOAD_MANIFEST).read_text(encoding="utf-8"))
    manifest["version"] = "0.1.0"
    (older / installer.PAYLOAD_MANIFEST).write_text(json.dumps(manifest), encoding="utf-8")
    installer.install(installer.Payload.from_folder(older, "0"), target, system)  # an older version is installed: the pages show an upgrade
    surface = OffscreenSurface(640, 380)
    try:
        ui = SetupUI(installer.Payload.from_folder(dist, "0"), system, licences(dist), target, launcher=lambda path: None)
        for _ in range(2):
            surface.frame(ui.draw)
        record(surface, "01_setup_welcome", "setup welcome page", "version, publisher, per-user install, no game data, unsigned note, licences")
        ui.page, ui.desktop = "options", True
        for _ in range(2):
            surface.frame(ui.draw)
        record(surface, "02_setup_options", "install location and options", "folder with Browse, desktop shortcut option, upgrade notice, Upgrade button")
        ui.page, ui.progress = "progress", ("Copying files", 243, 612)
        for _ in range(2):
            surface.frame(ui.draw)
        record(surface, "03_setup_progress", "installation progress", "progress bar with stage and count; automatic undo note")
        ui.page = "options"
        ui.start()
        ui.thread.join(60)
        for _ in range(2):
            surface.frame(ui.draw)
        record(surface, "04_setup_complete", "installation complete (upgrade in a sandbox)", "upgrade message, Start menu note, launch checkbox, Finish")
        uninstall = UninstallUI(target, system, (temp / "home",))
        for _ in range(2):
            surface.frame(uninstall.draw)
        record(surface, "05_uninstall_confirmation", "uninstall confirmation", "projects and game files kept; optional removal of settings and caches")
    finally:
        surface.close()


def editor_pages(temp: Path, record) -> None:
    import numpy as np

    from tools.btga_studio.core import environment, geometry as g, hub
    from tools.btga_studio.core.settings import Settings
    from tools.btga_studio.gui.app import StudioApp
    from tools.btga_studio.gui.offscreen import OffscreenUI

    app = StudioApp(Settings.open())
    ui = OffscreenUI(app, 1600, 960)
    c = app.controller
    try:
        for name in UNICODE_NAMES:
            path, _ = hub.create(c.library, name, "blank", 1024.0, directory=app.settings.projects_directory)
            app.settings.preferences.add_recent(str(path))
        app.settings.preferences.welcome_seen = True
        app.open_hub()
        for _ in range(4):
            ui.frame()
        record(ui, "06_unicode_project_names", "recent projects named in six scripts", "Latin, Greek, Cyrillic, Japanese, Korean and Chinese names drawn correctly")
        app.show_hub = False

        app.start_tour()
        ui.frame()
        app.open_example()
        for _ in range(4):
            ui.frame()
        record(ui, "07_tour_navigation", "guided tour, step 2", "tour panel explains looking around; the 3D view is outlined and labelled 'Step 2: here'")
        while app.tutorial.step is not None and app.tutorial.step.key != "spawn":
            app.tutorial.skip()
        for _ in range(3):
            ui.frame()
        record(ui, "08_tour_placement", "guided tour, placement step", "tour panel explains placing a spawn; the Assets panel is outlined and labelled")
        while app.tutorial.step is not None and app.tutorial.step.key != "validate":
            app.tutorial.skip()
        c.request_diagnostics(immediate=True)
        c.poll_diagnostics(wait=True)
        for _ in range(3):
            ui.frame()
        record(ui, "09_tour_validation", "guided tour, validation step", "tour panel explains the Validation tab; the tab is outlined and labelled")
        app.tutorial.stop()

        project = c.project
        app.open_gameplay("Missions & campaigns")
        mission = next(iter(project.missions))
        app.change("Rename mission", lambda: project.missions[mission].__setitem__("title", "Operation Северный ветер"))
        for _ in range(3):
            ui.frame()
        record(ui, "10a_gameplay_before_undo", "gameplay editor after renaming a mission", "the new mission name is shown; Undo gameplay change is enabled")
        app.undo()
        for _ in range(3):
            ui.frame()
        record(ui, "10b_gameplay_after_undo", "the same editor after Undo", "the original mission name is back; Redo is enabled")

        c.camera = g.Camera(position=np.array([0.0, 1500.0, 1500.0]), pitch=-math.radians(45))
        app.show_gameplay = False
        program = temp / "portable" / "BattleTanxStudio"
        program.mkdir(parents=True)
        app.environment = environment.Environment("portable", "0.2.0", "synthetic-revision", program, None, False)
        app.show_about = True
        for _ in range(3):
            ui.frame()
        record(ui, "11_about_portable", "About dialog in portable mode", "Portable (running from a folder), version, revision, locations, repair guidance")
        app.show_about = False
        ui.frame()
        installed = temp / "Local App Data" / "Programs" / "BattleTanx Studio"
        app.environment = environment.Environment("installed", "0.2.0", "synthetic-revision", installed, "0.2.0", False)
        app.show_about = True
        for _ in range(3):
            ui.frame()
        record(ui, "12_about_installed", "About dialog in installed mode", "Installed for this user, install location, repair guidance mentions Setup")
    finally:
        ui.close()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("output", type=Path)
    parser.add_argument("--review", type=Path)
    args = parser.parse_args(argv)
    if args.review:
        merge_review(args.output, args.review)
        return 0
    results = capture(args.output)
    print(json.dumps(results, indent=1))
    return 0 if all(r["ok"] for r in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
