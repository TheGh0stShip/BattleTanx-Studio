# PyInstaller spec for BattleTanxStudioSetup.exe (one file, windowed).  Run through build_windows.ps1, which sets
# BTGA_RELEASE_PREP (version resource, fonts) and BTGA_SETUP_PAYLOAD (the finished program folder to embed).
import os
from pathlib import Path

from PyInstaller.utils.hooks import collect_all

ROOT = Path(SPECPATH).resolve().parents[2]
PREP = Path(os.environ["BTGA_RELEASE_PREP"])
PAYLOAD = Path(os.environ["BTGA_SETUP_PAYLOAD"])
datas, binaries, hidden = collect_all("imgui_bundle")
_skip_paths = ("/demos_", "/demos/", "/sandbox/", "/playground/", "/notebooks/")
_skip_modules = (".demos_", ".demos.", ".sandbox.", ".playground.", ".notebooks", ".imgui_terminal", ".imgui_pydantic")
datas = [entry for entry in datas if not any(marker in (str(entry[0]) + "/" + str(entry[1])).replace("\\", "/").lower()
                                                for marker in _skip_paths)]
hidden = [name for name in hidden if not any(marker in name.lower() for marker in _skip_modules)]
datas += [(str(PAYLOAD), "payload"), (str(PREP / "data" / "fonts"), "data/fonts")]
hidden += ["OpenGL.platform.win32"]

analysis = Analysis(
    [str(ROOT / "tools/btga_studio/packaging/launcher_setup.py")],
    pathex=[str(ROOT)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hidden + ["tools.btga_studio.setup.main", "tools.btga_studio.setup.ui"],
    excludes=["tkinter", "matplotlib", "PIL", "pytest", "tools.btga_assets"],
    noarchive=False,
)
pyz = PYZ(analysis.pure)
EXE(pyz, analysis.scripts, analysis.binaries, analysis.datas, [], name="BattleTanxStudioSetup", console=False, debug=False, strip=False,
    upx=False, version=str(PREP / "setup_version_info.txt"))
