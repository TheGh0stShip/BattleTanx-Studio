<#
Build the BattleTanx Studio Windows release (developer use).

    powershell -NoProfile -NonInteractive -ExecutionPolicy Bypass -WindowStyle Hidden -File build_windows.ps1 -Source <clean source copy> -Output <dir> [-Revision <commit>]

Creates an isolated Python 3.13 virtual environment, installs the pinned
requirements, writes the version resource and build information, freezes
BattleTanxStudio.exe with PyInstaller (Windows GUI subsystem: no console),
adds the synthetic sample and the ROM-free starter example, and writes:

    <Output>\dist\BattleTanxStudio\                                the program folder (portable)
    <Output>\release\BattleTanxStudio-portable-x64.zip             deterministic archive of that folder
    <Output>\release\SHA256SUMS.txt, build-manifest.json, RELEASE-NOTES.txt, THIRD-PARTY-NOTICES.txt, README.txt

-FontDir names a folder holding the fonts listed in tools\btga_studio\fonts\fonts.json; each is checked
against its recorded SHA-256 before it is bundled. From Linux/WSL, create that
folder reproducibly with `python tools/fetch_studio_fonts.py private-work/toolchains/fonts`.

    <Output>\release\BattleTanxStudioSetup.exe                       the per-user installer (embeds the program folder)

It touches no game data, never launches the editor and never uploads anything.
#>
param(
    [Parameter(Mandatory = $true)][string]$Source,
    [Parameter(Mandatory = $true)][string]$Output,
    [string]$Revision = "unknown",
    [Parameter(Mandatory = $true)][string]$FontDir,
    [string]$Python = "py",
    [string]$PythonVersion = "3.13"
)
$ErrorActionPreference = "Stop"
$work = Join-Path $Output "build"
$venv = Join-Path $work "venv"
$prep = Join-Path $work "prep"
New-Item -ItemType Directory -Force -Path $work | Out-Null
if (-not (Test-Path (Join-Path $venv "Scripts\python.exe"))) {
    # Pin the interpreter: "py -3" picks the newest installed Python, which may lack wheels for the pinned requirements.
    if ($Python -eq "py") { & py "-$PythonVersion" -m venv $venv } else { & $Python -m venv $venv }
    if ($LASTEXITCODE -ne 0) { throw "could not create a Python $PythonVersion virtual environment" }
}
$vpy = Join-Path $venv "Scripts\python.exe"
& $vpy -m pip install --disable-pip-version-check --quiet -r (Join-Path $Source "tools\btga_studio\packaging\requirements-build.txt")
if ($LASTEXITCODE -ne 0) { throw "pip install failed" }
Push-Location $Source
try {
    $env:BTGA_STUDIO_FONT_DIR = $FontDir
    & $vpy -m tools.btga_studio.packaging.release prepare --out $prep --revision $Revision
    if ($LASTEXITCODE -ne 0) { throw "release preparation failed" }
    $env:BTGA_RELEASE_PREP = $prep
    & $vpy -m PyInstaller --noconfirm --clean --distpath (Join-Path $Output "dist") --workpath (Join-Path $work "pyinstaller") `
        (Join-Path $Source "tools\btga_studio\packaging\BTGAStudio.spec")
    if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed" }
    $app = Join-Path $Output "dist\BattleTanxStudio"
    $samples = Join-Path $app "samples"
    & $vpy -m tools.btga_studio.samples (Join-Path $samples "sample-catalog") --project (Join-Path $samples "sample.btgaproj")
    if ($LASTEXITCODE -ne 0) { throw "sample generation failed" }
    Copy-Item (Join-Path $Source "examples\starter-example.btgaproj") (Join-Path $samples "starter-example.btgaproj") -Force
    Copy-Item (Join-Path $Source "tools\btga_studio\packaging\README-windows.txt") (Join-Path $app "README.txt") -Force
    Copy-Item (Join-Path $Source "COPYING") (Join-Path $app "COPYING.txt") -Force
    & $vpy -m tools.btga_studio.packaging.release prepare-dist --dist $app
    if ($LASTEXITCODE -ne 0) { throw "payload preparation failed" }
    $env:BTGA_SETUP_PAYLOAD = $app
    & $vpy -m PyInstaller --noconfirm --clean --distpath (Join-Path $work "setup-dist") --workpath (Join-Path $work "setup-build") `
        (Join-Path $Source "tools\btga_studio\packaging\BattleTanxStudioSetup.spec")
    if ($LASTEXITCODE -ne 0) { throw "setup build failed" }
    & $vpy -m tools.btga_studio.packaging.release finalize --dist $app --out (Join-Path $Output "release") --revision $Revision `
        --setup (Join-Path $work "setup-dist\BattleTanxStudioSetup.exe")
    if ($LASTEXITCODE -ne 0) { throw "release finalisation failed" }
} finally {
    Pop-Location
}
Write-Output "built $(Join-Path $Output 'release')"
