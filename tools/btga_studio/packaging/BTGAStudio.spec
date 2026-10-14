# PyInstaller spec for BTGAStudio.exe (windowed, one folder).  Run through build_windows.ps1.
from pathlib import Path

from PyInstaller.utils.hooks import collect_all

ROOT = Path(SPECPATH).resolve().parents[2]
datas, binaries, hidden = collect_all("imgui_bundle")
hidden += ["OpenGL.platform.win32", "OpenGL.arrays.numpymodule", "OpenGL.arrays.ctypesarrays", "OpenGL.arrays.numbers"]

analysis = Analysis(
    [str(ROOT / "tools/btga_studio/packaging/launcher.py")],
    pathex=[str(ROOT)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hidden + ["tools.btga_studio.gui.app", "tools.btga_studio.gui.renderer", "tools.btga_studio.samples"],
    excludes=["tkinter", "matplotlib", "PIL", "pytest"],
    noarchive=False,
)
pyz = PYZ(analysis.pure)
exe = EXE(pyz, analysis.scripts, [], exclude_binaries=True, name="BTGAStudio", console=False, debug=False, strip=False, upx=False)
COLLECT(exe, analysis.binaries, analysis.datas, strip=False, upx=False, name="BTGAStudio")
