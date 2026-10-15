# PyInstaller spec for BattleTanxStudio.exe (windowed, one folder).  Run through build_windows.ps1, which sets
# BTGA_RELEASE_PREP to the folder written by `release.py prepare` (version resource and build.json).
import os
from pathlib import Path

from PyInstaller.utils.hooks import collect_all

ROOT = Path(SPECPATH).resolve().parents[2]
PREP = Path(os.environ["BTGA_RELEASE_PREP"])
datas, binaries, hidden = collect_all("imgui_bundle")
# collect_all also returns the dependency's example applications, source trees,
# notebooks and optional terminal integration.  Studio uses none of them; keeping
# them bloats the release, triggers irrelevant optional-dependency warnings and
# can leak unrelated demo text into the product audit.
_skip_paths = ("/demos_", "/demos/", "/sandbox/", "/playground/", "/notebooks/")
_skip_modules = (".demos_", ".demos.", ".sandbox.", ".playground.", ".notebooks", ".imgui_terminal", ".imgui_pydantic")
datas = [entry for entry in datas if not any(marker in (str(entry[0]) + "/" + str(entry[1])).replace("\\", "/").lower()
                                                for marker in _skip_paths)]
hidden = [name for name in hidden if not any(marker in name.lower() for marker in _skip_modules)]
hidden += ["OpenGL.platform.win32", "OpenGL.arrays.numpymodule", "OpenGL.arrays.ctypesarrays", "OpenGL.arrays.numbers"]
datas += [
    (str(ROOT / "src/code/frontend/800E8380_slot_asset_ranges.c"), "data"),  # Global Assault level table, needed to build N64 catalogs
    (str(PREP / "data" / "build.json"), "data"),
    (str(PREP / "data" / "fonts"), "data/fonts"),  # verified fonts and their licences (release.py prepare)
]

analysis = Analysis(
    [str(ROOT / "tools/btga_studio/packaging/launcher.py")],
    pathex=[str(ROOT)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hidden + ["tools.btga_studio.gui.app", "tools.btga_studio.gui.renderer", "tools.btga_studio.samples",
                            "tools.btga_studio.core.imports", "tools.btga_studio.core.osm", "tools.btga_studio.core.skyline",
                            "tools.btga_studio.core.scenario", "tools.btga_studio.core.items", "tools.btga_studio.core.bots",
                            "tools.btga_studio.core.actions", "tools.btga_studio.core.runtime", "tools.btga_studio.core.authoring",
                            "tools.btga_studio.core.navgraph", "tools.btga_studio.core.game_models", "tools.btga_studio.gui.authoring",
                            "tools.btga_studio.gui.drive_preview"],
    excludes=["tkinter", "matplotlib", "PIL", "pytest"],
    noarchive=False,
)
pyz = PYZ(analysis.pure)
exe = EXE(pyz, analysis.scripts, [], exclude_binaries=True, name="BattleTanxStudio", console=False, debug=False, strip=False, upx=False,
          version=str(PREP / "version_info.txt"))
COLLECT(exe, analysis.binaries, analysis.datas, strip=False, upx=False, name="BattleTanxStudio")
