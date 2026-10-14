<#
Build a portable BTGAStudio folder (BTGAStudio.exe + runtime) on Windows.

    powershell -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File build_windows.ps1 -Source <repo copy> -Output <dir>

Creates an isolated virtual environment, installs the pinned requirements,
freezes the editor with PyInstaller (windowed: no console), and adds a
synthetic sample catalog and project.  It touches no game data and never
launches the editor.
#>
param(
    [Parameter(Mandatory = $true)][string]$Source,
    [Parameter(Mandatory = $true)][string]$Output,
    [string]$Python = "py"
)
$ErrorActionPreference = "Stop"
$work = Join-Path $Output "build"
$venv = Join-Path $work "venv"
New-Item -ItemType Directory -Force -Path $work | Out-Null
if (-not (Test-Path (Join-Path $venv "Scripts\python.exe"))) {
    if ($Python -eq "py") { & py -3 -m venv $venv } else { & $Python -m venv $venv }
}
$vpy = Join-Path $venv "Scripts\python.exe"
& $vpy -m pip install --disable-pip-version-check --quiet -r (Join-Path $Source "tools\btga_studio\packaging\requirements-build.txt")
if ($LASTEXITCODE -ne 0) { throw "pip install failed" }
Push-Location $Source
try {
    & $vpy -m PyInstaller --noconfirm --clean --distpath (Join-Path $Output "dist") --workpath (Join-Path $work "pyinstaller") `
        (Join-Path $Source "tools\btga_studio\packaging\BTGAStudio.spec")
    if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed" }
    $app = Join-Path $Output "dist\BTGAStudio"
    $samples = Join-Path $app "samples"
    & $vpy -m tools.btga_studio.samples (Join-Path $samples "sample-catalog") --project (Join-Path $samples "sample.btgaproj")
    if ($LASTEXITCODE -ne 0) { throw "sample generation failed" }
    Copy-Item (Join-Path $Source "tools\btga_studio\packaging\README-windows.txt") (Join-Path $app "README.txt") -Force
} finally {
    Pop-Location
}
$zip = Join-Path $Output "BTGAStudio-windows-x64.zip"
if (Test-Path $zip) { Remove-Item $zip }
Compress-Archive -Path (Join-Path $Output "dist\BTGAStudio") -DestinationPath $zip
Write-Output "built $zip"
