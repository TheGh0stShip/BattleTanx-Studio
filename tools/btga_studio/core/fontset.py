"""Bundled UI fonts: discovery, integrity checks, licensing and staging for builds.

The font list, versions, provenance and SHA-256 hashes live in
``tools/btga_studio/fonts/fonts.json`` beside the licence texts.  The font
binaries are not kept in the repository; a build stages them from a verified
local source (``stage``) and ships them in ``data/fonts`` with their licences.

Lookup order for each font file (first file whose SHA-256 matches wins):
1. the program's bundled ``data/fonts`` (packaged builds);
2. ``BTGA_STUDIO_FONT_DIR`` (developer override);
3. the ``system_paths`` recorded in the manifest (distribution packages the
   manifest names, e.g. on the build machine).

A file with the wrong hash is never used.  If no primary font is found the
editor keeps Dear ImGui's built-in font (ASCII and Latin-1) and says so in
the log; text is never lost, only displayed with replacement glyphs.

Fallback strategy: the primary font (Latin, Greek, Cyrillic) is loaded
first; the fallback (CJK) is merged into it, so each character is drawn
from the first font that has it and the replacement glyph appears only for
characters neither font contains.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
from dataclasses import dataclass
from pathlib import Path

MANIFEST_DIR = Path(__file__).resolve().parents[1] / "fonts"


@dataclass(frozen=True)
class FontEntry:
    file: str
    family: str
    version: str
    role: str
    coverage: str
    license: str
    license_file: str
    sha256: str
    size: int
    obtained_from: str
    upstream: str
    package_url: str
    package_sha256: str
    package_path: str
    system_paths: tuple[str, ...]


def manifest_directory() -> Path:
    """The source tree's font manifest, or the copy a build staged beside the bundled fonts (frozen programs)."""

    if (MANIFEST_DIR / "fonts.json").is_file():
        return MANIFEST_DIR
    from .settings import bundled_data_directory

    return bundled_data_directory() / "fonts"


def manifest(directory: Path | None = None) -> list[FontEntry]:
    data = json.loads(((directory or manifest_directory()) / "fonts.json").read_text(encoding="utf-8"))
    return [FontEntry(f["file"], f["family"], f["version"], f["role"], f["coverage"], f["license"], f["license_file"], f["sha256"],
                      f["size"], f["obtained_from"], f["upstream"], f["package_url"], f["package_sha256"], f["package_path"],
                      tuple(f.get("system_paths", []))) for f in data["fonts"]]


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def candidates(entry: FontEntry, bundled: Path | None = None) -> list[Path]:
    out = []
    if bundled is not None:
        out.append(Path(bundled) / entry.file)
    override = os.environ.get("BTGA_STUDIO_FONT_DIR")
    if override:
        out.append(Path(override) / entry.file)
    out += [Path(p) for p in entry.system_paths]
    return out


def locate(entry: FontEntry, bundled: Path | None = None) -> Path | None:
    for path in candidates(entry, bundled):
        try:
            if path.is_file() and path.stat().st_size == entry.size and _sha256(path) == entry.sha256:
                return path
        except OSError:
            continue
    return None


def resolve(bundled: Path | None = None) -> tuple[list[tuple[FontEntry, Path]], list[str]]:
    """(found fonts in manifest order, problems)."""

    found, problems = [], []
    for entry in manifest():
        path = locate(entry, bundled)
        if path is None:
            problems.append(f"font {entry.family} {entry.version} was not found (or did not match its recorded SHA-256)")
        else:
            found.append((entry, path))
    return found, problems


def stage(destination: Path, bundled: Path | None = None) -> list[Path]:
    """Copy every verified font and its licence into ``destination`` (build step); raises if a font is missing."""

    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=True)
    written = []
    for entry in manifest():
        path = locate(entry, bundled)
        if path is None:
            raise FileNotFoundError(f"{entry.file}: no copy with SHA-256 {entry.sha256} (source: {entry.obtained_from})")
        shutil.copyfile(path, destination / entry.file)
        shutil.copyfile(manifest_directory() / entry.license_file, destination / entry.license_file)
        written += [destination / entry.file, destination / entry.license_file]
    shutil.copyfile(manifest_directory() / "fonts.json", destination / "fonts.json")
    return written + [destination / "fonts.json"]


def notices() -> str:
    lines = ["Fonts", "-----"]
    for entry in manifest():
        lines += [f"{entry.family} {entry.version} ({entry.file})", f"  Licence: {entry.license} - see {entry.license_file}",
                  f"  Upstream: {entry.upstream}", f"  Obtained from: {entry.obtained_from}", f"  SHA-256: {entry.sha256}", ""]
    return "\n".join(lines)
