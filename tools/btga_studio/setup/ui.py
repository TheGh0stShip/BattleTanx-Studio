"""Setup and uninstall pages drawn with Dear ImGui (no window code here, so the pages render off-screen in tests).

``SetupUI`` pages: welcome (version, publisher, no-game-data statement,
licences) -> options (folder, desktop shortcut, upgrade/repair/downgrade
notice) -> progress -> finished (launch checkbox) or problem (rolled back).
All work goes through ``core.installer``; ``system`` and ``launcher`` are
injectable so tests never touch the registry, Start menu or desktop.
"""

from __future__ import annotations

import os
import threading
from pathlib import Path

from imgui_bundle import imgui

from tools.btga_studio.core import installer

GREEN, AMBER, RED = (0.45, 0.9, 0.45, 1.0), (1.0, 0.78, 0.3, 1.0), (1.0, 0.42, 0.38, 1.0)
GAME_DATA = ("BattleTanx Studio contains no game data. To use maps and models from BattleTanx games, you add your own copy "
             "in the program; it is read on your computer and never copied into projects or packages.")


def _colour(rgba):
    return imgui.ImVec4(*rgba)


def display_path(path: Path) -> str:
    """Show the per-user default as %LOCALAPPDATA%\\... (what users recognise); other paths as given."""

    base = os.environ.get("LOCALAPPDATA")
    if base:
        try:
            relative = Path(path).relative_to(Path(base))
            return "%LOCALAPPDATA%\\" + "\\".join(relative.parts)
        except ValueError:
            pass
    return str(path)


def expand_path(text: str) -> Path:
    text = text.strip().strip('"')
    base = os.environ.get("LOCALAPPDATA")
    if base and text.upper().startswith("%LOCALAPPDATA%"):
        rest = text[len("%LOCALAPPDATA%"):].lstrip("\\/")
        return Path(base).joinpath(*[part for part in rest.replace("\\", "/").split("/") if part])
    return Path(text)


def header(title: str, subtitle: str) -> None:
    """A coloured band with the product name; the page content follows."""

    draw = imgui.get_window_draw_list()
    origin, width = imgui.get_cursor_screen_pos(), imgui.get_content_region_avail().x
    draw.add_rect_filled(imgui.ImVec2(origin.x - 8, origin.y - 8), imgui.ImVec2(origin.x + width + 8, origin.y + 46),
                         imgui.get_color_u32(imgui.ImVec4(0.16, 0.29, 0.48, 1.0)))
    imgui.text(title)
    imgui.text_colored(imgui.ImVec4(0.8, 0.86, 0.95, 1.0), subtitle)
    imgui.dummy(imgui.ImVec2(0, 14))


def _launch(path: Path) -> None:
    os.startfile(str(path))  # noqa: S606 (starts the installed program; no console)


class SetupUI:
    def __init__(self, payload: installer.Payload, system, licences: dict[str, str], target: Path | None = None, launcher=_launch,
                 dialogs=None):
        self.payload = payload
        self.system = system
        self.licences = licences
        self.target_text = display_path(Path(target or installer.default_target()))
        self.desktop = False
        self.launch_after = True
        self.allow_downgrade = False
        self.page = "welcome"
        self.licence_name: str | None = None
        self.progress = ("", 0, 1)
        self.error: str | None = None
        self.result: installer.Plan | None = None
        self.thread: threading.Thread | None = None
        self.launcher = launcher
        self.dialogs = dialogs
        self.finished = False  # the window should close

    @property
    def target(self) -> Path:
        return expand_path(self.target_text)

    def plan(self) -> installer.Plan:
        return installer.plan(self.payload, self.target, self.system, self.allow_downgrade)

    # -- actions ----------------------------------------------------------------------------
    def start(self) -> None:
        self.page = "progress"
        self.error = None

        def work():
            try:
                self.result = installer.install(self.payload, self.target, self.system, self.desktop, self.allow_downgrade,
                                                progress=lambda stage, done, total: setattr(self, "progress", (stage, done, total or 1)))
                self.page = "done"
            except installer.InstallError as error:
                self.error, self.page = str(error), "problem"
            except OSError as error:
                self.error, self.page = (f"{error}. Any changes were undone; the previous installation (if any) is unchanged."), "problem"

        self.thread = threading.Thread(target=work, name="setup-install", daemon=True)
        self.thread.start()

    def finish(self) -> None:
        if self.page == "done" and self.launch_after:
            self.launcher(self.target / installer.EXECUTABLE)
        self.finished = True

    # -- drawing ---------------------------------------------------------------------------------
    def draw(self) -> None:
        header(f"BattleTanx Studio {self.payload.version} Setup", f"Publisher: {installer.PUBLISHER}")
        if self.licence_name:
            self._licence()
            return
        getattr(self, "_" + self.page)()

    def _welcome(self) -> None:
        imgui.push_text_wrap_pos(0.0)
        imgui.text_wrapped("This installs BattleTanx Studio, a map and content editor for BattleTanx, for your Windows user account.")
        imgui.text_wrapped("No administrator rights are needed. Your projects, settings and game catalogs are kept when you upgrade "
                           "or uninstall.")
        imgui.spacing()
        imgui.text_colored(_colour(AMBER), "Game data is not included.")
        imgui.text_wrapped(GAME_DATA)
        imgui.text_wrapped("This program is not digitally signed, so Windows may show a warning before it runs.")
        imgui.pop_text_wrap_pos()
        imgui.spacing()
        imgui.text("Licences:")
        for name in self.licences:
            imgui.same_line()
            if imgui.small_button(f"{name}##licence"):
                self.licence_name = name
        imgui.spacing()
        if imgui.button("Next", imgui.ImVec2(120, 0)):
            self.page = "options"
        imgui.same_line()
        if imgui.button("Cancel"):
            self.finished = True

    def _options(self) -> None:
        imgui.text("Install to")
        imgui.set_next_item_width(-110)
        _, self.target_text = imgui.input_text("##target", self.target_text)
        imgui.same_line()
        if imgui.button("Browse...") and self.dialogs is not None:
            chosen = self.dialogs.select_folder("Choose where to install BattleTanx Studio", self.target.parent)
            if chosen:
                self.target_text = str(Path(chosen) / installer.APP_NAME)
        imgui.text_disabled("The default is in your user profile, so no administrator rights are needed.")
        _, self.desktop = imgui.checkbox("Add a desktop shortcut", self.desktop)
        imgui.text_disabled("A Start menu entry is always added. Windows Settings > Apps lists the program for removal.")
        plan = self.plan()
        message = {"fresh": "A new installation.", "upgrade": f"Upgrades version {plan.current} to {plan.new}.",
                   "reinstall": f"Version {plan.new} is already installed here. Installing again repairs missing or changed files.",
                   "downgrade": f"Version {plan.current} is installed, which is newer than {plan.new}."}[plan.kind]
        imgui.text_colored(_colour(AMBER if plan.kind == "downgrade" else GREEN), message)
        if plan.kind == "downgrade":
            _, self.allow_downgrade = imgui.checkbox("Install older version anyway", self.allow_downgrade)
        for blocker in plan.blockers:
            imgui.push_text_wrap_pos(0.0)
            imgui.text_colored(_colour(RED), blocker)
            imgui.pop_text_wrap_pos()
        imgui.spacing()
        if imgui.button("Back"):
            self.page = "welcome"
        imgui.same_line()
        imgui.begin_disabled(not plan.ok)
        label = {"fresh": "Install", "upgrade": "Upgrade", "reinstall": "Repair", "downgrade": "Install"}[plan.kind]
        if imgui.button(label, imgui.ImVec2(120, 0)):
            self.start()
        imgui.end_disabled()
        imgui.same_line()
        if imgui.button("Cancel"):
            self.finished = True

    def _progress(self) -> None:
        stage, done, total = self.progress
        imgui.text("Installing BattleTanx Studio...")
        imgui.progress_bar(min(1.0, done / max(1, total)), imgui.ImVec2(-1, 0), f"{stage} {done}/{total}")
        imgui.text_disabled("If anything goes wrong, every change is undone automatically.")

    def _done(self) -> None:
        kind = self.result.kind if self.result else "fresh"
        imgui.text_colored(_colour(GREEN), {"fresh": "BattleTanx Studio is installed.", "upgrade": "BattleTanx Studio was upgraded.",
                                            "reinstall": "BattleTanx Studio was repaired.", "downgrade": "BattleTanx Studio is installed."}[kind])
        imgui.text_wrapped("Start it from the Start menu" + (" or the desktop shortcut." if self.desktop else "."))
        _, self.launch_after = imgui.checkbox("Start BattleTanx Studio now", self.launch_after)
        if imgui.button("Finish", imgui.ImVec2(120, 0)):
            self.finish()

    def _problem(self) -> None:
        imgui.text_colored(_colour(RED), "The installation did not finish.")
        imgui.push_text_wrap_pos(0.0)
        imgui.text_wrapped(self.error or "")
        imgui.pop_text_wrap_pos()
        if imgui.button("Back"):
            self.page = "options"
        imgui.same_line()
        if imgui.button("Close"):
            self.finished = True

    def _licence(self) -> None:
        imgui.text(self.licence_name)
        imgui.begin_child("licence-text", imgui.ImVec2(0, -32))
        imgui.text_unformatted(self.licences[self.licence_name])
        imgui.end_child()
        if imgui.button("Back"):
            self.licence_name = None


class UninstallUI:
    """The confirmation shown by ``BattleTanxStudio.exe --uninstall`` (Windows Settings > Apps)."""

    def __init__(self, target: Path, system, user_data: tuple):
        self.target, self.system, self.user_data = Path(target), system, user_data
        self.remove_data = False
        self.result: installer.UninstallResult | None = None
        self.error: str | None = None
        self.finished = False

    def draw(self) -> None:
        header("Remove BattleTanx Studio", f"Publisher: {installer.PUBLISHER}")
        imgui.push_text_wrap_pos(0.0)
        if self.result is not None:
            imgui.text_colored(_colour(GREEN), "BattleTanx Studio was removed.")
            if self.result.kept:
                imgui.text_wrapped(f"{len(self.result.kept)} file(s) you added to the program folder were kept.")
            if self.result.removed_user_data:
                imgui.text_wrapped("Your Studio settings, catalogs, caches and logs were removed as requested.")
            else:
                imgui.text_wrapped("Your settings, catalogs and logs were kept, in case you install again.")
            imgui.pop_text_wrap_pos()
            if imgui.button("Close"):
                self.finished = True
            return
        imgui.text_wrapped("Remove BattleTanx Studio from this computer?")
        imgui.text_wrapped("Your projects are never removed, and your game files are never touched.")
        imgui.pop_text_wrap_pos()
        _, self.remove_data = imgui.checkbox("Also remove my Studio settings, catalogs, caches and logs", self.remove_data)
        if self.error:
            imgui.text_colored(_colour(RED), self.error)
        if imgui.button("Remove", imgui.ImVec2(120, 0)):
            try:
                self.result = installer.uninstall(self.target, self.system, remove_user_data=self.remove_data, user_data=self.user_data,
                                                  running_from_target=True)
            except (installer.InstallError, OSError) as error:
                self.error = str(error)
        imgui.same_line()
        if imgui.button("Cancel"):
            self.finished = True
