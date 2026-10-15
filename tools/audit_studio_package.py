"""Audit a packaged BattleTanx Studio folder (Windows PyInstaller output) without running it.

Checks:

* ``BattleTanxStudio.exe`` is a PE image for the Windows GUI subsystem (2), so no
  console window opens with it;
* required runtime files exist (the executable, the internal runtime folder,
  README, the synthetic sample and the ROM-free starter example);
* no proprietary input, evidence or private file types are present
  (ROM/disc images, archives, crash dumps, logs, captures); PyInstaller's
  ``base_library.zip`` is the one permitted archive and its members are
  checked individually;
* no file contains a forbidden text fragment (for example the build user's
  profile path); fragments are matched as ASCII and UTF-16LE bytes.

It also writes a deterministic manifest (relative path, size, SHA-256) and
can compare two manifests, naming every differing file.

    python tools/audit_studio_package.py DIST_DIR --manifest OUT.json [--forbid TEXT ...]
    python tools/audit_studio_package.py --compare A.json B.json

Exit status 0 when the audit passes (or the manifests are identical), 1 otherwise.
Memory: files are scanned in 4 MiB windows with overlap, so any package size works.
"""

from __future__ import annotations

import argparse
import json
import struct
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.studio_evidence import compare_manifests, manifest  # noqa: E402

EXECUTABLE = "BattleTanxStudio.exe"
REQUIRED = (EXECUTABLE, "_internal", "README.txt", "COPYING.txt", "THIRD-PARTY-NOTICES.txt", "_internal/data/build.json",
            "_internal/data/800E8380_slot_asset_ranges.c", "_internal/data/fonts/fonts.json", "_internal/data/fonts/DejaVuSans.ttf",
            "payload-manifest.json", "samples/sample.btgaproj", "samples/sample-catalog/catalog.json",
            "samples/starter-example.btgaproj")
FORBIDDEN_SUFFIXES = {".z64", ".n64", ".v64", ".rom", ".iso", ".cue", ".bin", ".chd", ".img", ".zip", ".7z", ".rar", ".dmp",
                      ".mdmp", ".pcap", ".pcapng", ".log", ".btgapkg"}
# PyInstaller's own standard-library archive is a required runtime file; its members are scanned instead.
RUNTIME_ARCHIVES = {"_internal/base_library.zip"}
SUBSYSTEMS = {2: "windows-gui", 3: "windows-console"}
WINDOW = 4 << 20


def pe_subsystem(path: Path) -> str:
    data = Path(path).read_bytes()[:4096]
    if data[:2] != b"MZ":
        raise ValueError(f"{path.name} is not a PE image")
    offset = struct.unpack_from("<I", data, 0x3C)[0]
    if data[offset:offset + 4] != b"PE\0\0":
        raise ValueError(f"{path.name} has no PE signature")
    optional = offset + 24
    magic = struct.unpack_from("<H", data, optional)[0]
    if magic not in (0x10B, 0x20B):
        raise ValueError(f"{path.name} has an unknown optional header")
    value = struct.unpack_from("<H", data, optional + 68)[0]
    return SUBSYSTEMS.get(value, f"subsystem-{value}")


VERSION_KEYS = ("CompanyName", "FileDescription", "FileVersion", "OriginalFilename", "ProductName", "ProductVersion")


def version_strings(path: Path) -> dict:
    """StringFileInfo values from a PE version resource (UTF-16LE key, NUL, 32-bit padding, value)."""

    data = Path(path).read_bytes()
    out = {}
    for key in VERSION_KEYS:
        marker = key.encode("utf-16-le") + b"\x00\x00"
        position = data.rfind(marker)
        if position < 0:
            continue
        start = position + len(marker)
        start += (-start) % 4  # values are DWORD aligned
        end = start
        while end + 1 < len(data) and data[end:end + 2] != b"\x00\x00":
            end += 2
        out[key] = data[start:end].decode("utf-16-le", errors="replace")
    return out


def _contains(path: Path, needles: list[bytes], opener=None) -> list[bytes]:
    found = set()
    longest = max(len(n) for n in needles)
    tail = b""
    with (opener() if opener else open(path, "rb")) as handle:
        while True:
            block = handle.read(WINDOW)
            if not block:
                break
            window = tail + block
            lowered = window.lower()
            for needle in needles:
                if needle in lowered:
                    found.add(needle)
            tail = window[-longest:]
    return sorted(found)


def audit(dist: Path, forbid: list[str]) -> dict:
    dist = Path(dist)
    problems = []
    for name in REQUIRED:
        if not (dist / name).exists():
            problems.append(f"missing required {name}")
    subsystem = None
    version = {}
    if (dist / EXECUTABLE).is_file():
        subsystem = pe_subsystem(dist / EXECUTABLE)
        if subsystem != "windows-gui":
            problems.append(f"{EXECUTABLE} uses the {subsystem} subsystem")
        version = version_strings(dist / EXECUTABLE)
        if not version.get("ProductVersion") or version.get("OriginalFilename") != EXECUTABLE:
            problems.append(f"{EXECUTABLE} has no complete version resource")
    needles = []
    for text in forbid:
        needles += [text.lower().encode("utf-8"), text.lower().encode("utf-16-le")]
    files = sorted(p for p in dist.rglob("*") if p.is_file())
    leaks = {}
    for path in files:
        relative = path.relative_to(dist).as_posix()
        if path.suffix.lower() in FORBIDDEN_SUFFIXES and relative not in RUNTIME_ARCHIVES:
            problems.append(f"forbidden file type {relative}")
        if needles:
            hits = _contains(path, needles)
            if hits:
                leaks[relative] = len(hits)
        if relative in RUNTIME_ARCHIVES:
            with zipfile.ZipFile(path) as archive:
                for member in archive.infolist():
                    if Path(member.filename).suffix.lower() in FORBIDDEN_SUFFIXES:
                        problems.append(f"forbidden file type {relative}:{member.filename}")
                    if needles:
                        hits = _contains(path, needles, lambda m=member: archive.open(m))
                        if hits:
                            leaks[f"{relative}:{member.filename}"] = len(hits)
    for relative, count in sorted(leaks.items()):
        problems.append(f"{relative} contains {count} forbidden fragment encoding(s)")
    return {"ok": not problems, "problems": problems, "subsystem": subsystem, "version": version, "files": len(files),
            "bytes": sum(p.stat().st_size for p in files)}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("dist", type=Path, nargs="?")
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--forbid", action="append", default=[], help="text that must not occur in any file (case-insensitive)")
    parser.add_argument("--compare", nargs=2, type=Path, metavar=("A", "B"))
    args = parser.parse_args(argv)
    if args.compare:
        first, second = (json.loads(p.read_text(encoding="utf-8")) for p in args.compare)
        difference = compare_manifests(first, second)
        print(json.dumps(difference, indent=1))
        return 0 if not any(difference.values()) else 1
    result = audit(args.dist, args.forbid)
    if args.manifest:
        args.manifest.write_text(json.dumps(manifest(args.dist), indent=1, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(result, indent=1, sort_keys=True))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
