"""Release tooling for the Windows build (developer use; called by build_windows.ps1).

    python -m tools.btga_studio.packaging.release prepare --out BUILD_DIR [--revision R]
        writes BUILD_DIR/version_info.txt (Windows version resource) and BUILD_DIR/data/build.json
        (also both version resources and the verified fonts in BUILD_DIR/data/fonts)
    python -m tools.btga_studio.packaging.release prepare-dist --dist DIST_DIR
        writes THIRD-PARTY-NOTICES.txt and payload-manifest.json into the program folder
    python -m tools.btga_studio.packaging.release finalize --dist DIST_DIR --out RELEASE_DIR [--setup SETUP_EXE] [--revision R]
        writes the release folder: BattleTanxStudioSetup.exe, BattleTanxStudio-portable-x64.zip (deterministic),
        SHA256SUMS.txt, build-manifest.json, RELEASE-NOTES.txt, THIRD-PARTY-NOTICES.txt, README.txt

Determinism: zip members are sorted, stored with a fixed 1980-01-01 time and
fixed permissions, so the archive bytes depend only on file contents.
Inputs are local folders; nothing is uploaded.  Version text comes from
``tools.btga_studio.__version__``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import platform
import re
import sys
import zipfile
from importlib import metadata
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.btga_studio import __version__  # noqa: E402
from tools.btga_studio.core.atomic import atomic_write_bytes, atomic_write_text  # noqa: E402

APP = "BattleTanxStudio"
BUNDLED_DISTRIBUTIONS = ("imgui-bundle", "numpy", "PyOpenGL", "pyinstaller")
FIXED_TIME = (1980, 1, 1, 0, 0, 0)


def version_tuple(version: str = __version__) -> tuple[int, int, int, int]:
    numbers = [int(n) for n in re.findall(r"\d+", version)[:4]]
    return tuple((numbers + [0, 0, 0, 0])[:4])


def version_resource(version: str = __version__, revision: str | None = None, name: str = APP, description: str = "BattleTanx Studio") -> str:
    """PyInstaller ``--version-file`` text (VSVersionInfo)."""

    numbers = version_tuple(version)
    strings = {"CompanyName": "BattleTanx Studio contributors", "FileDescription": description, "FileVersion": version,
               "InternalName": name, "LegalCopyright": "See COPYING and THIRD-PARTY-NOTICES.txt", "OriginalFilename": f"{name}.exe",
               "ProductName": "BattleTanx Studio", "ProductVersion": version + (f" ({revision[:12]})" if revision else "")}
    entries = ",\n          ".join(f"StringStruct({k!r}, {v!r})" for k, v in strings.items())
    return f"""VSVersionInfo(
  ffi=FixedFileInfo(filevers={numbers}, prodvers={numbers}, mask=0x3f, flags=0x0, OS=0x40004, fileType=0x1, subtype=0x0, date=(0, 0)),
  kids=[
    StringFileInfo([StringTable('040904B0', [
          {entries}])]),
    VarFileInfo([VarStruct('Translation', [1033, 1200])])
  ]
)
"""


def build_info(revision: str | None) -> dict:
    return {"build": f"{__version__} Windows x64", "revision": revision or "unknown", "python": platform.python_version(),
            "built": "release build"}


def notices() -> str:
    """Licences of every bundled third-party distribution plus Python itself."""

    parts = ["BattleTanx Studio bundles the following third-party software.", ""]
    for name in BUNDLED_DISTRIBUTIONS:
        try:
            dist = metadata.distribution(name)
        except metadata.PackageNotFoundError:
            parts += [f"== {name}: not installed in the build environment ==", ""]
            continue
        meta = dist.metadata
        licence = meta.get("License-Expression") or meta.get("License") or ""
        parts += [f"== {meta['Name']} {dist.version} ==", f"Licence: {licence.splitlines()[0] if licence else 'see below'}",
                  f"Home page: {meta.get('Home-page') or meta.get('Project-URL') or 'n/a'}"]
        for file in sorted(dist.files or [], key=str):
            if re.search(r"(LICEN[CS]E|COPYING|NOTICE)", file.name, re.I) and file.suffix.lower() not in (".py", ".pyc"):
                try:
                    text = Path(dist.locate_file(file)).read_bytes().decode("utf-8", errors="replace").strip()
                except OSError:
                    continue
                parts += ["", f"--- {file.name} ---", text]
        if name == "pyinstaller":
            parts.append("PyInstaller's bootloader is distributed under the GPL with an exception that permits bundling it with "
                         "programs under any licence.")
        parts.append("")
    parts += [f"== Python {platform.python_version()} ==", "Licence: Python Software Foundation License Version 2",
              "See https://docs.python.org/3/license.html", ""]
    from tools.btga_studio.core import fontset

    parts += [fontset.notices(), "The full font licences are in _internal/data/fonts beside the program.", ""]
    return "\n".join(parts)


def deterministic_zip(folder: Path, output: Path, prefix: str) -> Path:
    folder = Path(folder)
    files = sorted(p for p in folder.rglob("*") if p.is_file())
    from io import BytesIO

    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for path in files:
            info = zipfile.ZipInfo(f"{prefix}/{path.relative_to(folder).as_posix()}", FIXED_TIME)
            info.external_attr = 0o644 << 16
            info.create_system = 0
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, path.read_bytes())
    return atomic_write_bytes(output, buffer.getvalue())


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


PORTABLE = f"{APP}-portable-x64.zip"
SETUP = f"{APP}Setup.exe"
RELEASE_FILES = (SETUP, PORTABLE, "SHA256SUMS.txt", "build-manifest.json", "RELEASE-NOTES.txt", "THIRD-PARTY-NOTICES.txt", "README.txt")


def prepare_dist(dist: Path) -> dict:
    """Write the notices and the payload manifest into the program folder (before it is zipped or embedded in the setup)."""

    from tools.btga_studio.core import installer

    dist = Path(dist)
    if not (dist / f"{APP}.exe").is_file():
        raise SystemExit(f"{APP}.exe is missing from the program folder")
    atomic_write_text(dist / "THIRD-PARTY-NOTICES.txt", notices())
    (dist / installer.PAYLOAD_MANIFEST).unlink(missing_ok=True)
    payload = installer.Payload.from_folder(dist, __version__)
    atomic_write_text(dist / installer.PAYLOAD_MANIFEST, json.dumps(payload.manifest(), indent=1, sort_keys=True) + "\n")
    return payload.manifest()


def finalize(dist: Path, out: Path, revision: str | None, setup: Path | None = None) -> dict:
    """Assemble the release folder: setup, portable ZIP, checksums, manifest, notes, notices and README (nothing else)."""

    import shutil

    dist, out = Path(dist), Path(out)
    if not (dist / f"{APP}.exe").is_file():
        raise SystemExit(f"{APP}.exe is missing from the program folder")
    out.mkdir(parents=True, exist_ok=True)
    archive = deterministic_zip(dist, out / PORTABLE, f"{APP}-{__version__}")
    if setup is not None:
        shutil.copyfile(setup, out / SETUP)
    for name in ("THIRD-PARTY-NOTICES.txt", "README.txt"):
        shutil.copyfile(dist / name, out / name)
    notes = Path(__file__).resolve().parent / "RELEASE-NOTES.txt"
    atomic_write_text(out / "RELEASE-NOTES.txt", notes.read_text(encoding="utf-8").replace("{version}", __version__))
    files = {p.relative_to(dist).as_posix(): {"sha256": sha256(p), "size": p.stat().st_size} for p in sorted(dist.rglob("*")) if p.is_file()}
    artifacts = {p.name: {"sha256": sha256(p), "size": p.stat().st_size} for p in sorted(out.glob("*"))
                 if p.is_file() and p.name not in ("SHA256SUMS.txt", "build-manifest.json")}
    from tools.btga_studio.core import installer

    manifest = {"product": "BattleTanx Studio", "version": __version__, "revision": revision or "unknown", "platform": "windows-x64",
                "entry_point": f"{APP}.exe", "setup": SETUP if setup is not None else None, "upgrade_code": installer.UPGRADE_CODE,
                "publisher": installer.PUBLISHER, "python": platform.python_version(), "signed": False,
                "toolchain": {n: _version(n) for n in BUNDLED_DISTRIBUTIONS},
                "archive": {"name": archive.name, "sha256": sha256(archive), "size": archive.stat().st_size},
                "artifacts": artifacts, "files": files}
    atomic_write_text(out / "build-manifest.json", json.dumps(manifest, indent=1, sort_keys=True) + "\n")
    sums = [f"{sha256(p)}  {p.name}" for p in sorted(out.glob("*")) if p.is_file() and p.name != "SHA256SUMS.txt"]
    atomic_write_text(out / "SHA256SUMS.txt", "\n".join(sums) + "\n")
    unexpected = sorted(p.name for p in out.iterdir() if p.name not in RELEASE_FILES)
    if unexpected:
        raise SystemExit(f"unexpected files in the release folder: {unexpected}")
    return manifest


def _version(name: str) -> str | None:
    try:
        return metadata.version(name)
    except metadata.PackageNotFoundError:
        return None


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    prepare = sub.add_parser("prepare")
    prepare.add_argument("--out", type=Path, required=True)
    prepare.add_argument("--revision")
    dist = sub.add_parser("prepare-dist")
    dist.add_argument("--dist", type=Path, required=True)
    final = sub.add_parser("finalize")
    final.add_argument("--dist", type=Path, required=True)
    final.add_argument("--out", type=Path, required=True)
    final.add_argument("--setup", type=Path)
    final.add_argument("--revision")
    args = parser.parse_args(argv)
    if args.command == "prepare":
        from tools.btga_studio.core import fontset

        atomic_write_text(args.out / "version_info.txt", version_resource(__version__, args.revision))
        atomic_write_text(args.out / "setup_version_info.txt",
                          version_resource(__version__, args.revision, f"{APP}Setup", "BattleTanx Studio Setup"))
        atomic_write_text(args.out / "data" / "build.json", json.dumps(build_info(args.revision), indent=1, sort_keys=True) + "\n")
        fontset.stage(args.out / "data" / "fonts")  # verified by SHA-256; fails the build if a font is missing
    elif args.command == "prepare-dist":
        manifest = prepare_dist(args.dist)
        print(f"payload: {len(manifest['files'])} files")
    else:
        manifest = finalize(args.dist, args.out, args.revision, args.setup)
        print(f"{manifest['archive']['name']} {manifest['archive']['sha256']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
