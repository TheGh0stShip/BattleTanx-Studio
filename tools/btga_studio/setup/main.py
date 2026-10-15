"""Entry point of BattleTanxStudioSetup.exe (frozen) and ``python -m tools.btga_studio.setup.main`` (developers).

    BattleTanxStudioSetup.exe                      graphical setup
    BattleTanxStudioSetup.exe --silent [--target DIR] [--desktop] [--allow-downgrade] [--report FILE]
    BattleTanxStudioSetup.exe --self-check FILE    verify the embedded payload (no window, nothing installed)

Test-only: ``--sandbox ROOT`` records shortcuts and the uninstall entry as files
under ROOT instead of the real Start menu and registry (used by hidden
filesystem tests; never by users).

The payload (the portable program folder plus ``payload-manifest.json``) is
embedded under ``payload/`` in the frozen setup; ``--payload DIR`` points at a
folder when running from source.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from tools.btga_studio.core import installer


def payload_directory(explicit: Path | None) -> Path:
    if explicit:
        return Path(explicit)
    base = getattr(sys, "_MEIPASS", None)
    if base:
        return Path(base) / "payload"
    raise SystemExit("no payload: pass --payload DIR when running from source")


def licences(root: Path) -> dict[str, str]:
    out = {}
    for label, name in (("BattleTanx Studio", "COPYING.txt"), ("Third-party notices", "THIRD-PARTY-NOTICES.txt")):
        path = root / name
        if path.is_file():
            out[label] = path.read_text(encoding="utf-8", errors="replace")
    return out


def self_check(payload: installer.Payload, report: Path) -> int:
    problems = []
    for relative, info in sorted(payload.files.items()):
        path = payload.root / relative
        if not path.is_file() or installer.sha256(path) != info["sha256"]:
            problems.append(relative)
    result = {"ok": not problems, "version": payload.version, "files": len(payload.files), "damaged": problems,
              "upgrade_code": installer.UPGRADE_CODE, "has_program": installer.EXECUTABLE in payload.files}
    result["ok"] = result["ok"] and result["has_program"]
    Path(report).write_text(json.dumps(result, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    return 0 if result["ok"] else 1


class FolderPicker:
    def select_folder(self, title: str, start: Path | None = None) -> Path | None:
        from imgui_bundle import portable_file_dialogs as pfd

        folder = pfd.select_folder(title, str(start) if start else "").result()
        return Path(folder) if folder else None


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="BattleTanxStudioSetup", description="Install BattleTanx Studio for this user")
    parser.add_argument("--payload", type=Path)
    parser.add_argument("--silent", action="store_true")
    parser.add_argument("--target", type=Path)
    parser.add_argument("--desktop", action="store_true")
    parser.add_argument("--allow-downgrade", action="store_true")
    parser.add_argument("--report", type=Path)
    parser.add_argument("--self-check", type=Path)
    parser.add_argument("--sandbox", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    root = payload_directory(args.payload)
    payload = installer.Payload.from_folder(root, "0")
    if args.self_check:
        return self_check(payload, args.self_check)
    system = installer.SandboxSystem(args.sandbox) if args.sandbox else installer.WindowsSystem()
    if args.silent:
        result = {"ok": False}
        try:
            plan = installer.install(payload, args.target or installer.default_target(), system, args.desktop, args.allow_downgrade)
            result = {"ok": True, "kind": plan.kind, "from": plan.current, "to": plan.new, "warnings": plan.warnings}
        except installer.InstallError as error:
            result = {"ok": False, "refused": str(error)}
        except OSError as error:
            result = {"ok": False, "rolled_back": str(error)}
        if args.report:
            Path(args.report).write_text(json.dumps(result, indent=1, sort_keys=True) + "\n", encoding="utf-8")
        return 0 if result["ok"] else 1
    from imgui_bundle import hello_imgui, immapp

    from tools.btga_studio.gui import fonts
    from tools.btga_studio.setup.ui import SetupUI

    ui = SetupUI(payload, system, licences(root), args.target, dialogs=FolderPicker())

    def gui():
        ui.draw()
        if ui.finished:
            hello_imgui.get_runner_params().app_shall_exit = True

    params = hello_imgui.RunnerParams()
    params.app_window_params.window_title = "BattleTanx Studio Setup"
    params.app_window_params.window_geometry.size = (640, 380)
    params.callbacks.show_gui = gui
    params.callbacks.load_additional_fonts = lambda: fonts.load()
    params.fps_idling.enable_idling = True
    immapp.run(params)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
